# OpticalWala.com — Architecture Design

## DB Foundation (what we have)
- products: id, product_name, brand, main_group, category, material, index_value, 
            coating, colour, gst_percent, wear_schedule, gender, unit, box_size
- inventory_stock: product_id, sph/cyl/axis/add_power, quantity, mrp, 
                   selling_price, purchase_rate, eye_side, batch_no
- pricing_tiers: tier_name, discount_percent
- parties: existing customer records

## What needs to be added to DB (migrations)
1. products: online_price, online_active, online_description, sort_order, tags
2. product_images: id, product_id, image_url, image_b64, is_primary, sort_order
3. online_customers: id, name, mobile, email, password_hash, created_at
4. online_carts: id, customer_id, product_id/stock_id, qty, created_at
5. online_orders: id, customer_id, address_id, total, status, payment_status
6. online_order_lines: order_id, product_id, qty, price, eye_side, power data
7. customer_addresses: id, customer_id, name, line1, city, state, pincode, phone
8. promo_codes: code, discount_type(PCT/FLAT), value, min_order, uses_left, valid_to

## Module structure
modules/online_store/
  ├── store_app.py          — main Streamlit app (public-facing)
  ├── store_auth.py         — OTP login, JWT sessions
  ├── store_catalog.py      — product listing, search, filters
  ├── store_product.py      — product detail, power selection
  ├── store_cart.py         — cart management  
  ├── store_orders.py       — order placement, history
  ├── store_payment.py      — Razorpay integration
  └── store_admin.py        — admin: set online_price, toggle online_active, upload images

## Pages
/ Homepage — hero, categories, featured products
/shop — catalog with filters (category, brand, power range, price)
/product/:id — detail page, power selector, add to cart
/cart — cart review, promo code
/checkout — address, payment
/orders — order history, tracking
/account — profile, addresses
