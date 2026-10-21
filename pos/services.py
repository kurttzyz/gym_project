import hashlib
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4
from django.core import signing
from django.db import transaction
from tenant_data.models import Location
from .models import Product, StockBalance, StockMovement, Sale, SaleLine, SaleRefund


class PosError(Exception):
    pass


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")) or amount >= Decimal("100000000000000"):
            raise ValueError
        return amount.quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation):
        raise PosError("Enter a valid nonnegative PHP amount with at most two decimal places.") from None


def adjust_stock(alias, product_id, location_id, delta, note, request_id, actor_id):
    if type(delta) is not int or delta == 0 or abs(delta) > 1000000 or not note.strip() or len(note.strip()) > 300:
        raise PosError("Enter a nonzero quantity change and a reason (maximum 300 characters).")
    fingerprint = digest([str(product_id), location_id, delta, note.strip(), actor_id])
    with transaction.atomic(using=alias):
        Location.objects.select_for_update().get(pk=location_id)
        previous = StockMovement.objects.filter(pk=request_id).first()
        if previous:
            if previous.kind != "adjustment" or previous.fingerprint != fingerprint:
                raise PosError("This adjustment request was already used with different details.")
            return previous
        Product.objects.select_for_update().get(pk=product_id)
        balance, _ = StockBalance.objects.get_or_create(product_id=product_id, location_id=location_id)
        after = balance.quantity + delta
        if not 0 <= after <= 2147483647:
            raise PosError("This adjustment would make stock negative or exceed the supported quantity.")
        balance.quantity = after
        balance.save(update_fields=["quantity"])
        return StockMovement.objects.create(id=request_id, balance=balance, delta=delta, balance_after=after,
            kind="adjustment", actor_id=actor_id, note=note.strip(), fingerprint=fingerprint)


def quote_cart(gym, actor_id, location_id, items, gym_name):
    branch = Location.objects.get(pk=location_id)
    if not branch.is_active:
        raise PosError("This branch is disabled for new sales.")
    quantities = {}
    if not items or len(items) > 20:
        raise PosError("Choose between one and twenty product rows.")
    for product_id, quantity in items:
        if type(quantity) is not int or not 1 <= quantity <= 999:
            raise PosError("Quantities must be whole units from 1 to 999 per product.")
        key = str(UUID(str(product_id)))
        quantities[key] = quantities.get(key, 0) + quantity
        if quantities[key] > 999:
            raise PosError("A product cannot exceed 999 units in one sale.")
    products = list(Product.objects.filter(pk__in=quantities, is_active=True).order_by("pk"))
    if len(products) != len(quantities):
        raise PosError("A product is unavailable. Review the cart again.")
    balances = {str(row.product_id): row.quantity for row in StockBalance.objects.filter(location=branch, product_id__in=quantities)}
    lines = []
    for product in products:
        quantity = quantities[str(product.pk)]
        if balances.get(str(product.pk), 0) < quantity:
            raise PosError(f"Not enough stock for {product.name} at this branch.")
        lines.append({"p": str(product.pk), "q": quantity, "price": str(product.price), "sku": product.sku, "name": product.name})
    data = {"g": gym.database_key.hex, "u": actor_id, "b": branch.pk, "r": str(uuid4()), "items": lines, "gym": gym_name}
    return signing.dumps(data, salt="gym.pos.checkout.v1." + gym.database_key.hex), data


def decode_quote(gym, actor_id, token, *, max_age=None):
    try:
        if not isinstance(token, str) or len(token) > 65536:
            raise ValueError
        data = signing.loads(token, salt="gym.pos.checkout.v1." + gym.database_key.hex, max_age=max_age)
        if data["g"] != gym.database_key.hex or data["u"] != actor_id or not 1 <= len(data["items"]) <= 20:
            raise ValueError
        UUID(data["r"])
        return data
    except (signing.BadSignature, ValueError, KeyError, TypeError):
        raise PosError("This checkout quote is invalid or expired. Review your cart again.") from None


def commit_sale(alias, gym, actor_id, token, method, tendered=None, reference="", note="", confirmed=False):
    data = decode_quote(gym, actor_id, token)
    if method not in {"cash", "bank"} or len(reference.strip()) > 120 or len(note.strip()) > 300:
        raise PosError("Enter valid payment details.")
    if method == "cash":
        tendered = money(tendered)
        reference, note = "", ""
    elif not confirmed or not reference.strip() or not note.strip():
        raise PosError("Verify the bank transfer and enter its reference and verification note.")
    payment = {"method": method, "tendered": str(tendered) if method == "cash" else "", "reference": reference.strip().casefold(), "note": note.strip()}
    fingerprint = digest({"quote": data, "payment": payment})
    with transaction.atomic(using=alias):
        branch = Location.objects.select_for_update().get(pk=data["b"])
        previous = Sale.objects.filter(pk=data["r"]).first()
        if previous:
            if previous.fingerprint != fingerprint:
                raise PosError("This checkout was already recorded with different payment details. Review its existing receipt.")
            return previous
        # An exact retry can return its original receipt even after quote expiry;
        # new sales must have a fresh quote and live prices/availability.
        decode_quote(gym, actor_id, token, max_age=600)
        if not branch.is_active:
            raise PosError("This branch is disabled for new sales.")
        products = {str(product.pk): product for product in Product.objects.select_for_update().filter(
            pk__in=[item["p"] for item in data["items"]]).order_by("pk")}
        prepared, total = [], Decimal("0.00")
        for item in data["items"]:
            product = products.get(item["p"])
            if not product or not product.is_active or (str(product.price), product.sku, product.name) != (item["price"], item["sku"], item["name"]):
                raise PosError("A product or its price changed. Review the cart again before taking payment.")
            balance = StockBalance.objects.filter(product=product, location=branch).first()
            if not balance or balance.quantity < item["q"]:
                raise PosError(f"Not enough stock for {product.name}. No sale was recorded.")
            line_total = product.price * item["q"]
            total += line_total
            prepared.append((product, balance, item["q"], line_total))
        if method == "cash" and tendered < total:
            raise PosError("Cash received is less than the sale total.")
        if method == "bank":
            if total == 0:
                raise PosError("Use cash/no money collected for a zero-total sale.")
            if Sale.objects.filter(external_reference__iexact=reference.strip()).exists():
                raise PosError("This bank transfer reference is already recorded on another sale.")
            tendered = total
        sale = Sale.objects.create(id=UUID(data["r"]), fingerprint=fingerprint, location=branch, branch_name=branch.name,
            branch_address=branch.address, gym_name=data["gym"], actor_id=actor_id, total=total, method=method,
            tendered=tendered, change=tendered-total if method == "cash" else Decimal("0.00"),
            external_reference=reference.strip(), verification_note=note.strip())
        for product, balance, quantity, line_total in prepared:
            SaleLine.objects.create(sale=sale, product=product, sku=product.sku, name=product.name,
                quantity=quantity, unit_price=product.price, total=line_total)
            balance.quantity -= quantity
            balance.save(update_fields=["quantity"])
            StockMovement.objects.create(balance=balance, delta=-quantity, balance_after=balance.quantity,
                kind="sale", actor_id=actor_id, note=f"Sale {sale.number}", sale=sale)
        return sale


def refund_sale(alias, sale_id, actor_id, reference, note, restock):
    reference, note = reference.strip(), note.strip()
    if not reference or len(reference) > 120 or not note or len(note) > 300 or type(restock) is not bool:
        raise PosError("Enter a unique refund reference and reason, and choose whether to restock.")
    preview = Sale.objects.get(pk=sale_id)
    with transaction.atomic(using=alias):
        Location.objects.select_for_update().get(pk=preview.location_id)
        sale = Sale.objects.select_for_update().get(pk=sale_id)
        previous = SaleRefund.objects.filter(sale=sale).first()
        if previous:
            if (previous.reference.casefold(), previous.note, previous.restocked) != (reference.casefold(), note, restock):
                raise PosError("This sale was already refunded. Its original refund record was preserved.")
            return previous
        if SaleRefund.objects.filter(reference__iexact=reference).exists():
            raise PosError("That refund reference is already used on another sale.")
        refund = SaleRefund.objects.create(sale=sale, actor_id=actor_id, amount=sale.total,
            reference=reference, note=note, restocked=restock)
        if restock:
            lines = list(sale.lines.order_by("product_id"))
            # Same lock order as checkout/adjustments, including across branches.
            list(Product.objects.select_for_update().filter(pk__in=[line.product_id for line in lines]).order_by("pk"))
            for line in lines:
                balance = StockBalance.objects.get(product_id=line.product_id, location_id=sale.location_id)
                if balance.quantity + line.quantity > 2147483647:
                    raise PosError("Returned stock exceeds the supported quantity. No refund was recorded.")
                balance.quantity += line.quantity
                balance.save(update_fields=["quantity"])
                StockMovement.objects.create(balance=balance, delta=line.quantity, balance_after=balance.quantity,
                    kind="refund", actor_id=actor_id, note=f"Full refund: {note}", sale=sale)
        return refund
