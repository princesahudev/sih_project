import json
from decimal import Decimal, InvalidOperation

from django.contrib.auth.hashers import check_password, make_password
from django.db import connection, transaction
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie

from .models import Account, Order, OrderItem, Product


def home(request):
    """Renders the public landing page."""
    return render(request, 'index.html')


def login_view(request):
    """Renders the role-based login page (Farmer, Consumer, Bulk, Delivery, Admin)."""
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        phone = request.POST.get('phone', '').strip()
        clean_phone = ''.join(c for c in phone if c.isdigit())[-10:]
        name = request.POST.get('name', '').strip()
        password = request.POST.get('password', '')

        account = None
        if email and (clean_phone or phone):
            account = Account.objects.filter(email__iexact=email, phone__in=[phone, clean_phone]).first()
        if not account and email:
            account = Account.objects.filter(email__iexact=email).first()
        if not account and (clean_phone or phone):
            account = Account.objects.filter(phone__in=[phone, clean_phone]).first()
        if not account and name and email:
            account = Account.objects.filter(name__iexact=name, email__iexact=email).first()

        if account and check_password(password, account.password_hash):
            request.session['account_id'] = account.pk
            next_url = request.GET.get('next') or request.POST.get('next')
            if next_url:
                return redirect(next_url)
            return redirect(_dashboard_for_role(account.role))
        return render(request, 'login.html', {
            'login_error': 'Email, mobile number, password, or role is incorrect.',
            'submitted_name': name,
            'submitted_email': email,
            'submitted_phone': phone,
        }, status=401)
    return render(request, 'login.html')



def signin_view(request):
    """Renders the OTP-based sign-in / registration page."""
    return render(request, 'signin.html')


def register_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST is required.'}, status=405)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        payload = request.POST

    email = payload.get('email', '').strip().lower()
    phone = payload.get('phone', '').strip()
    password = payload.get('password', '')
    name = payload.get('name', '').strip()
    role = payload.get('role', 'consumer')
    if not email or not phone or not password:
        return JsonResponse({'error': 'Email, mobile number, and password are required.'}, status=400)
    if not Account.objects.filter(email=email).exists() and not Account.objects.filter(phone=phone).exists():
        account = Account.objects.create(
            name=name,
            email=email,
            phone=phone,
            role=role if role in dict(Account.ROLE_CHOICES) else 'consumer',
            password_hash=make_password(password),
        )
        request.session['account_id'] = account.pk
        return JsonResponse({'redirect': '/' + str(_dashboard_for_role(account.role).replace('_', '-')) + '/'})
    return JsonResponse({'error': 'An account already exists with this email or mobile number.'}, status=409)


def profile_view(request):
    account_id = request.session.get('account_id')
    account = Account.objects.filter(pk=account_id).first() if account_id else None
    if not account:
        return redirect(f'/login/?next=/profile/')
    return render(request, 'profile.html', {'account': account})


def logout_view(request):
    request.session.flush()
    return redirect('login')


def _dashboard_for_role(role):
    return {
        'farmer': 'farmer_dashboard',
        'consumer': 'marketplace',
        'bulk': 'marketplace',
        'delivery': 'delivery_dashboard',
        'admin': 'admin_dashboard',
    }.get(role, 'marketplace')


def marketplace_view(request):
    """Renders the marketplace browsing and cart page."""
    products = Product.objects.filter(is_active=True).values(
        'id', 'name', 'category', 'description', 'unit', 'price', 'stock_quantity', 'farmer_name'
    )
    return render(request, 'marketplace.html', {
        'marketplace_products': list(products),
        'cart_items': list(request.session.get('cart_items', {}).values()),
    })


@ensure_csrf_cookie
def checkout_view(request):
    """Renders the checkout and order placement page."""
    return render(request, 'checkout.html', {
        'cart_items': list(request.session.get('cart_items', {}).values()),
    })


def cart_sync_view(request):
    """Stores the current marketplace cart in the user's session."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST is required.'}, status=405)
    try:
        payload = json.loads(request.body)
    except (TypeError, json.JSONDecodeError):
        return JsonResponse({'error': 'Invalid cart payload.'}, status=400)

    items = {}
    for raw_item in payload.get('items', []):
        try:
            product_id = int(raw_item['id'])
            quantity = int(raw_item['quantity'])
            price = Decimal(str(raw_item['price']))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return JsonResponse({'error': 'Invalid cart item.'}, status=400)
        if product_id <= 0 or quantity <= 0 or price < 0:
            return JsonResponse({'error': 'Invalid cart item.'}, status=400)
        items[str(product_id)] = {
            'id': product_id,
            'name': str(raw_item.get('name', '')).strip(),
            'unit': str(raw_item.get('unit', 'kg')).strip() or 'kg',
            'farmer': str(raw_item.get('farmer', '')).strip(),
            'price': str(price),
            'icon': str(raw_item.get('icon', '')),
            'cat': str(raw_item.get('cat', 'veg')),
            'img': str(raw_item.get('img', raw_item.get('image', ''))).strip(),
            'quantity': quantity,
        }
    request.session['cart_items'] = items
    request.session.modified = True
    return JsonResponse({'item_count': sum(item['quantity'] for item in items.values())})


def create_order_view(request):
    """Creates an order from the session cart and clears it only on success."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST is required.'}, status=405)
    cart_items = request.session.get('cart_items', {})
    if not cart_items:
        return JsonResponse({'error': 'Your cart is empty.'}, status=400)
    try:
        payload = json.loads(request.body)
        customer_name = str(payload.get('name', '')).strip()
        customer_phone = str(payload.get('phone', '')).strip()
        address = str(payload.get('address', '')).strip()
        cod_fee = Decimal('15') if payload.get('payment_method') == 'cod' else Decimal('0')
        discount = Decimal('10') if payload.get('promo') == 'HARVEST10' else Decimal('0')
    except (TypeError, json.JSONDecodeError, InvalidOperation):
        return JsonResponse({'error': 'Invalid order payload.'}, status=400)
    if not customer_name or not customer_phone or not address:
        return JsonResponse({'error': 'Name, phone, and address are required.'}, status=400)

    try:
        with transaction.atomic():
            order = Order.objects.create(
                customer_name=customer_name,
                customer_phone=customer_phone,
                delivery_address=address,
            )
            subtotal = Decimal('0')
            for item in cart_items.values():
                quantity = Decimal(str(item['quantity']))
                unit_price = Decimal(str(item['price']))
                product, _ = Product.objects.get_or_create(
                    pk=item['id'],
                    defaults={
                        'name': item['name'],
                        'category': item.get('cat', 'veg'),
                        'unit': item['unit'],
                        'price': unit_price,
                        'stock_quantity': 0,
                        'farmer_name': item['farmer'],
                    },
                )
                if product.price != unit_price:
                    return JsonResponse({'error': f'Price changed for {product.name}.'}, status=409)
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    quantity=quantity,
                    unit_price=product.price,
                )
                subtotal += quantity * product.price
            delivery_fee = Decimal('0') if subtotal >= Decimal('500') else Decimal('40')
            order.total_amount = max(Decimal('0'), subtotal + delivery_fee + cod_fee - discount)
            order.save(update_fields=['total_amount'])
    except (KeyError, InvalidOperation, ValueError) as exc:
        return JsonResponse({'error': f'Invalid cart item: {exc}'}, status=400)

    request.session.pop('cart_items', None)
    request.session.modified = True
    return JsonResponse({'order_id': order.pk, 'total': str(order.total_amount)})


@ensure_csrf_cookie
def farmer_dashboard_view(request):
    """Renders the farmer inventory and earnings dashboard."""
    account_id = request.session.get('account_id')
    account = Account.objects.filter(pk=account_id).first() if account_id else None
    if not account:
        return redirect(f'/login/?next=/farmer-dashboard/')
    if account.role != 'farmer':
        return redirect(_dashboard_for_role(account.role))
    farmer_products = Product.objects.filter(farmer_name__in=[account.name, account.email])
    orders = Order.objects.filter(items__product__farmer_name__in=[account.name, account.email]).distinct()
    return render(request, 'farmer-dashboard.html', {
        'account': account,
        'farmer_products': farmer_products,
        'orders': orders,
    })



def _farmer_account(request):
    account_id = request.session.get('account_id')
    account = Account.objects.filter(pk=account_id).first() if account_id else None
    return account if account and account.role == 'farmer' else None


def farmer_listing_create_view(request):
    """Creates a listing for the farmer currently signed in."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST is required.'}, status=405)
    account = _farmer_account(request)
    if not account:
        return JsonResponse({'error': 'Farmer login required.'}, status=403)
    try:
        payload = json.loads(request.body)
        name = str(payload.get('name', '')).strip()
        quantity = Decimal(str(payload.get('quantity', '0')))
        price = Decimal(str(payload.get('price', '0')))
    except (TypeError, json.JSONDecodeError, InvalidOperation):
        return JsonResponse({'error': 'Invalid listing payload.'}, status=400)
    if not name or quantity <= 0 or price < 0:
        return JsonResponse({'error': 'Crop, quantity, and price are required.'}, status=400)

    next_product_id = (Product.objects.aggregate(max_id=Max('id'))['max_id'] or 0) + 1
    product = Product.objects.create(
        id=next_product_id,
        name=name,
        category='Produce',
        unit='kg',
        price=price,
        stock_quantity=quantity,
        farmer_name=account.name or account.email,
    )
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT setval(pg_get_serial_sequence('core_product', 'id'), %s, true)",
                [product.pk],
            )
    return JsonResponse({
        'id': product.pk,
        'name': product.name,
        'quantity': str(product.stock_quantity),
        'price': str(product.price),
        'farmer': product.farmer_name,
    }, status=201)


def farmer_order_accept_view(request, order_id):
    """Accepts a pending order containing one of the farmer's products."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST is required.'}, status=405)
    account = _farmer_account(request)
    if not account:
        return JsonResponse({'error': 'Farmer login required.'}, status=403)
    order = Order.objects.filter(
        pk=order_id,
        status='pending',
        items__product__farmer_name=account.name or account.email,
    ).first()
    if not order:
        return JsonResponse({'error': 'Pending farmer order not found.'}, status=404)
    order.status = 'confirmed'
    order.save(update_fields=['status'])
    return JsonResponse({'order_id': order.pk, 'status': order.status})


def delivery_dashboard_view(request):
    """Renders the delivery partner route and tasks dashboard."""
    account_id = request.session.get('account_id')
    account = Account.objects.filter(pk=account_id).first() if account_id else None
    if not account:
        return redirect('/login/?next=/delivery-dashboard/')
    if account.role != 'delivery':
        return redirect(_dashboard_for_role(account.role))
    return render(request, 'delivery-dashboard.html', {'account': account})


def admin_dashboard_view(request):
    """Renders the admin platform oversight and verification dashboard."""
    account_id = request.session.get('account_id')
    account = Account.objects.filter(pk=account_id).first() if account_id else None
    if not account:
        return redirect('/login/?next=/admin-dashboard/')
    if account.role != 'admin':
        return redirect(_dashboard_for_role(account.role))
    # Provide summary stats for the admin dashboard KPI cards
    total_orders = Order.objects.count()
    pending_orders = Order.objects.filter(status='pending').count()
    total_accounts = Account.objects.count()
    total_products = Product.objects.filter(is_active=True).count()
    recent_orders = Order.objects.select_related().prefetch_related('items__product')[:10]
    return render(request, 'admin-dashboard.html', {
        'account': account,
        'total_orders': total_orders,
        'pending_orders': pending_orders,
        'total_accounts': total_accounts,
        'total_products': total_products,
        'recent_orders': recent_orders,
    })


def traceability_view(request):
    """Renders the farm-to-door batch traceability and tracking page."""
    return render(request, 'traceability.html')


def notifications_view(request):
    """Renders the unified notifications center."""
    return render(request, 'notifications.html')
