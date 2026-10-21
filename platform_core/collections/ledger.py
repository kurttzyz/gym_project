"""Central ledger. All balance mutations serialize on the gym/environment wallet."""
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.utils import timezone
from platform_core.models import GymWallet, WalletEntry, CentralCollection, PayoutDestination, GymWithdrawal


class LedgerError(Exception):
    pass


def amount(value, *, signed=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result != result.quantize(Decimal('.01')) or not result or (result < 0 and not signed) or abs(result) > Decimal('9999999999.99'):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise LedgerError('Enter a valid amount with at most two decimal places.') from None


def wallet_for(gym, environment):
    if environment not in {'sandbox', 'production'}:
        raise LedgerError('Invalid environment.')
    return GymWallet.objects.get_or_create(organization=gym, environment=environment)[0]


def post(wallet, key, action, note, actor_id=None, pending=0, available=0, reserved=0):
    """Caller holds wallet lock in a central transaction. Entries are append-only in app flows."""
    existing = WalletEntry.objects.filter(key=key).first()
    if existing:
        if (existing.wallet_id, existing.pending_delta, existing.available_delta, existing.reserved_delta) != (wallet.pk, pending, available, reserved):
            raise LedgerError('Ledger operation conflicts with an existing entry.')
        return existing
    if min(wallet.pending + pending, wallet.available + available, wallet.reserved + reserved) < 0:
        raise LedgerError('Insufficient funds in the selected balance.')
    wallet.pending += pending
    wallet.available += available
    wallet.reserved += reserved
    wallet.save(update_fields=['pending', 'available', 'reserved'])
    return WalletEntry.objects.create(wallet=wallet, key=key, action=action, note=note[:500], actor_id=actor_id,
        pending_delta=pending, available_delta=available, reserved_delta=reserved)


def release(collection_id, actor_id, evidence):
    if not evidence.strip() or len(evidence) > 400:
        raise LedgerError('A settlement verification reference is required.')
    preview = CentralCollection.objects.get(pk=collection_id)
    wallet = wallet_for(preview.organization, preview.environment)
    with transaction.atomic(using='default'):
        wallet = GymWallet.objects.select_for_update().get(pk=wallet.pk)
        collection = CentralCollection.objects.select_for_update().get(pk=collection_id)
        if collection.status != 'paid' or collection.net_amount is None:
            raise LedgerError('Only a confirmed collection can be released.')
        if collection.released_at:
            return collection
        post(wallet, 'release:'+str(collection.pk), 'release', evidence, actor_id,
             pending=-collection.net_amount, available=collection.net_amount)
        collection.released_at = timezone.now()
        collection.save(update_fields=['released_at'])
        return collection


def request_withdrawal(gym, environment, destination_id, value, actor_id, request_key):
    value = amount(value)
    wallet = wallet_for(gym, environment)
    with transaction.atomic(using='default'):
        wallet = GymWallet.objects.select_for_update().get(pk=wallet.pk)
        existing = GymWithdrawal.objects.filter(request_key=request_key).first()
        if existing:
            if existing.wallet_id != wallet.pk or existing.amount != value or existing.destination_id != destination_id:
                raise LedgerError('This request key was already used.')
            return existing
        destination = PayoutDestination.objects.select_for_update().filter(pk=destination_id, organization=gym,
            is_active=True, verified_at__isnull=False).first()
        if not destination:
            raise LedgerError('Select a payout destination verified by the platform.')
        withdrawal = GymWithdrawal.objects.create(wallet=wallet, destination=destination, requested_by_id=actor_id,
            request_key=request_key, amount=value, bank=destination.bank, account_name=destination.account_name,
            account_number=destination.account_number)
        post(wallet, 'reserve:'+str(withdrawal.pk), 'reserve', 'Withdrawal requested', actor_id, available=-value, reserved=value)
        return withdrawal


def transition(withdrawal_id, action, actor_id, note, reference='', *, gym=None):
    if not note.strip() or len(note) > 500:
        raise LedgerError('A reason or verification note is required.')
    preview = GymWithdrawal.objects.get(pk=withdrawal_id)
    with transaction.atomic(using='default'):
        wallet = GymWallet.objects.select_for_update().get(pk=preview.wallet_id)
        row = GymWithdrawal.objects.select_for_update().get(pk=withdrawal_id)
        if gym is not None and (wallet.organization_id != gym.pk or action != 'cancel'):
            raise LedgerError('Withdrawal not available.')
        target = {'approve':'approved', 'process':'processing', 'pay':'paid', 'reject':'rejected', 'cancel':'cancelled'}.get(action)
        if not target:
            raise LedgerError('Invalid withdrawal action.')
        if row.status == target:
            if action == 'pay' and row.payout_reference != reference.strip():
                raise LedgerError('Payout already recorded with a different reference.')
            return row
        allowed = {'approve':{'requested'}, 'process':{'approved'}, 'pay':{'processing'},
                   'reject':{'requested','approved'}, 'cancel':{'requested'}}
        if row.status not in allowed[action]:
            raise LedgerError('This withdrawal cannot perform that action. Processing payouts must be reconciled before funds can be released.')
        if action == 'pay':
            reference = reference.strip()
            if not reference or len(reference) > 120 or GymWithdrawal.objects.filter(payout_reference__iexact=reference).exclude(pk=row.pk).exists():
                raise LedgerError('A unique verified payout reference is required.')
            row.payout_reference = reference
            post(wallet, 'payout:'+str(row.pk), 'payout', note, actor_id, reserved=-row.amount)
        elif action in {'reject','cancel'}:
            post(wallet, 'unreserve:'+str(row.pk), 'unreserve', note, actor_id, available=row.amount, reserved=-row.amount)
        else:
            post(wallet, action+':'+str(row.pk), action, note, actor_id)
        row.status, row.note, row.reviewed_by_id = target, note, actor_id
        row.save()
        return row


def adjust(wallet_id, bucket, value, actor_id, reference, note):
    value = amount(value, signed=True)
    if bucket not in {'pending','available'} or not reference.strip() or len(reference.strip().casefold()) > 100 or not note.strip() or len(note) > 400:
        raise LedgerError('Select pending/available and supply a unique reference and reason.')
    with transaction.atomic(using='default'):
        wallet = GymWallet.objects.select_for_update().get(pk=wallet_id)
        return post(wallet, 'adjust:'+reference.strip().casefold(), 'adjustment', note, actor_id, **{bucket:value})
