CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_created
    ON orders(created_at DESC, order_id DESC);

CREATE INDEX IF NOT EXISTS idx_orders_customer_created
    ON orders(customer_id, created_at DESC, order_id DESC);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    body_hash TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
