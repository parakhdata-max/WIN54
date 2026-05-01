SELECT id::text AS product_id, product_name FROM products WHERE main_group ILIKE '%ophthalmic%' AND is_active = true LIMIT 5;
