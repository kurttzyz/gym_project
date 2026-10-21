from functools import wraps
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import OuterRef, Subquery, IntegerField, Value, F, Sum
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from config.database_router import gym_database
from gyms.databases import GymDatabaseUnavailable, ready_database
from gyms.models import Membership
from gym_core.memberships.views import owner_page
from tenant_data.models import Location
from websites.services import website_for_request
from .models import Product, StockBalance, StockMovement, Sale, SaleRefund
from .forms import ProductForm, BranchForm, StockAdjustmentForm, CartFormSet, PaymentForm, RefundForm, ReportFilterForm
from .services import PosError, quote_cart, decode_quote, commit_sale, adjust_stock, refund_sale


def cashier_page(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        access = Membership.objects.filter(organization=request.tenant, user=request.user, is_active=True,
                                            role__in={"owner", "staff", "manager"}).first() if request.tenant else None
        if not access:
            raise Http404
        request.pos_owner = access.role == "owner"
        try:
            with gym_database(ready_database(request.tenant)):
                return view(request, *args, **kwargs)
        except (GymDatabaseUnavailable, DatabaseError, OSError):
            return render(request, "gyms/unavailable.html", status=503)
    return wrapped


@owner_page
def products(request):
    query = request.GET.get("q", "").strip()[:160]
    items = Product.objects.all()
    if query:
        from django.db.models import Q
        items = items.filter(Q(name__icontains=query) | Q(sku__icontains=query))
    return render(request, "pos/products.html", {"page": Paginator(items, 25).get_page(request.GET.get("page")), "query": query})


@owner_page
def edit_product(request, product_id=None):
    product = get_object_or_404(Product, pk=product_id) if product_id else None
    form = ProductForm(request.POST if request.method == "POST" else None, instance=product)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic(using=ready_database(request.tenant)):
            if product:
                Product.objects.select_for_update().get(pk=product.pk)
            form.save()
        messages.success(request, "Product saved. Existing sale receipts were preserved.")
        return redirect("pos-products")
    return render(request, "pos/edit_product.html", {"form": form, "product": product})


@owner_page
@never_cache
def inventory(request):
    form = BranchForm(request.GET or None)
    branch = None
    if form.is_bound:
        if form.is_valid():
            branch = form.cleaned_data["location"]
    else:
        branch = Location.objects.order_by("-is_active", "name").first()
        if branch:
            form.fields["location"].initial = branch.pk
    items = Product.objects.none()
    movements = StockMovement.objects.none()
    if branch:
        balance = StockBalance.objects.filter(location=branch, product=OuterRef("pk")).values("quantity")[:1]
        items = Product.objects.annotate(on_hand=Coalesce(Subquery(balance, output_field=IntegerField()), Value(0)))
        if request.GET.get("low_only") == "on":
            items = items.filter(on_hand__lte=F("low_stock_threshold"))
        movements = StockMovement.objects.filter(balance__location=branch).select_related("balance__product")[:30]
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "pos/inventory.html", {"form": form, "branch": branch,
        "page": Paginator(items, 25).get_page(request.GET.get("page")), "movements": movements,
        "filter_query": query.urlencode(), "low_only": request.GET.get("low_only") == "on"})


@owner_page
@never_cache
def adjustment(request):
    form = StockAdjustmentForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            adjust_stock(ready_database(request.tenant), form.cleaned_data["product"].pk, form.cleaned_data["location"].pk,
                         form.cleaned_data["delta"], form.cleaned_data["note"], form.cleaned_data["request_id"], request.user.pk)
        except PosError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Stock change recorded with its operator and reason.")
            return redirect("pos-inventory")
    return render(request, "pos/adjustment.html", {"form": form})


@cashier_page
def checkout(request):
    branch_form = BranchForm(request.POST if request.method == "POST" else None, active_only=True)
    cart = CartFormSet(request.POST if request.method == "POST" else None, prefix="cart")
    if request.method == "POST" and all([branch_form.is_valid(), cart.is_valid()]):
        items = [(row.cleaned_data["product"].pk, row.cleaned_data["quantity"]) for row in cart if row.cleaned_data]
        try:
            token, data = quote_cart(request.tenant, request.user.pk, branch_form.cleaned_data["location"].pk,
                                     items, website_for_request(request).display_name)
        except PosError as error:
            branch_form.add_error(None, str(error))
        else:
            return render_review(request, token, data)
    return render(request, "pos/checkout.html", {"branch_form": branch_form, "cart": cart,
        "has_branches": Location.objects.filter(is_active=True).exists(), "has_products": Product.objects.filter(is_active=True).exists()})


def render_review(request, token, data, form=None):
    lines, total = [], Decimal("0.00")
    for item in data["items"]:
        line_total = Decimal(item["price"]) * item["q"]
        lines.append({**item, "line_total": line_total})
        total += line_total
    return render(request, "pos/review.html", {"lines": lines, "total": total,
        "branch": get_object_or_404(Location, pk=data["b"]),
        "form": form if form is not None else PaymentForm(initial={"quote": token, "method": "cash", "tendered": total})})


@cashier_page
@require_POST
def confirm_sale(request):
    form = PaymentForm(request.POST)
    token = request.POST.get("quote", "")
    try:
        data = decode_quote(request.tenant, request.user.pk, token)
    except PosError as error:
        messages.error(request, str(error))
        return redirect("pos-checkout")
    if form.is_valid():
        try:
            sale = commit_sale(ready_database(request.tenant), request.tenant, request.user.pk, token,
                form.cleaned_data["method"], form.cleaned_data["tendered"], form.cleaned_data["reference"],
                form.cleaned_data["note"], form.cleaned_data["confirmed"])
        except (PosError, IntegrityError) as error:
            form.add_error(None, str(error) if isinstance(error, PosError) else "That payment reference or checkout is already recorded. Review the sales history before retrying.")
        else:
            return redirect("pos-receipt", sale_id=sale.pk)
    return render_review(request, token, data, form)


def accessible_sales(request):
    sales = Sale.objects.select_related("refund")
    return sales if request.pos_owner else sales.filter(actor_id=request.user.pk)


@cashier_page
def sales(request):
    items = accessible_sales(request)
    return render(request, "pos/sales.html", {"page": Paginator(items, 25).get_page(request.GET.get("page")), "owner": request.pos_owner})


@cashier_page
def receipt(request, sale_id):
    sale = get_object_or_404(accessible_sales(request).prefetch_related("lines"), pk=sale_id)
    return render(request, "pos/receipt.html", {"sale": sale, "owner": request.pos_owner})


@owner_page
@never_cache
def refund(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related("refund").prefetch_related("lines"), pk=sale_id)
    form = RefundForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            refund_sale(ready_database(request.tenant), sale.pk, request.user.pk,
                        form.cleaned_data["reference"], form.cleaned_data["note"], form.cleaned_data["restock"])
        except (PosError, IntegrityError) as error:
            form.add_error(None, str(error) if isinstance(error, PosError) else "That refund reference is already recorded. No stock changes were applied.")
        else:
            messages.success(request, "Full refund recorded. The original sale and its receipt were retained.")
            return redirect("pos-receipt", sale_id=sale.pk)
    return render(request, "pos/refund.html", {"sale": sale, "form": form})


@owner_page
@never_cache
def report(request):
    form = ReportFilterForm(request.GET or {"date": timezone.localdate().isoformat()})
    sales_qs, refunds_qs = Sale.objects.none(), SaleRefund.objects.none()
    if form.is_valid():
        sales_qs = Sale.objects.filter(created_at__date=form.cleaned_data["date"])
        refunds_qs = SaleRefund.objects.filter(created_at__date=form.cleaned_data["date"])
        if form.cleaned_data["location"]:
            sales_qs = sales_qs.filter(location=form.cleaned_data["location"])
            refunds_qs = refunds_qs.filter(sale__location=form.cleaned_data["location"])
    gross = sales_qs.aggregate(total=Sum("total"))["total"] or Decimal("0.00")
    returned = refunds_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    return render(request, "pos/report.html", {"form": form, "gross": gross, "refunded": returned, "net": gross-returned,
        "sale_count": sales_qs.count(), "refund_count": refunds_qs.count()})
