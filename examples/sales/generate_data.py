"""Regenerate the example CSV files. Deterministic: same seed, same data."""
import csv
import datetime as dt
import random
from pathlib import Path

OUT = Path(__file__).parent / "data"
random.seed(20261117)

products = [
    (1, "Trail 500", "Bikes", "Red", 420.0, 780.0),
    (2, "Trail 700", "Bikes", "Black", 610.0, 1150.0),
    (3, "City Cruiser", "Bikes", "Blue", 300.0, 540.0),
    (4, "Helmet Pro", "Accessories", "Red", 18.0, 45.0),
    (5, "Bottle Cage", "Accessories", "Black", 3.0, 9.0),
    (6, "LED Light Set", "Accessories", "Silver", 9.0, 24.0),
    (7, "Rain Jacket", "Clothing", "Blue", 31.0, 79.0),
    (8, "Cycling Gloves", "Clothing", "Black", 7.0, 22.0),
    (9, "Team Jersey", "Clothing", "Red", 16.0, 49.0),
]
customers = [
    (1, "An Phat Retail", "Vietnam", "Corporate"),
    (2, "Saigon Wheels", "Vietnam", "Corporate"),
    (3, "Minh Tran", "Vietnam", "Consumer"),
    (4, "Bangkok Bikes", "Thailand", "Corporate"),
    (5, "Niran S.", "Thailand", "Consumer"),
    (6, "Lion City Cycles", "Singapore", "Corporate"),
    (7, "Mei Ling", "Singapore", "Consumer"),
    (8, "Jakarta Riders", "Indonesia", "Corporate"),
]

OUT.mkdir(exist_ok=True)

with open(OUT / "product.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["product_key", "product_name", "category", "color", "unit_cost", "list_price"])
    w.writerows(products)

with open(OUT / "customer.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["customer_key", "customer_name", "country", "segment"])
    w.writerows(customers)

start, end = dt.date(2023, 1, 1), dt.date(2024, 12, 31)
with open(OUT / "date.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "year", "quarter", "month_no", "month", "year_month"])
    d = start
    while d <= end:
        w.writerow([d.isoformat(), d.year, f"Q{(d.month - 1) // 3 + 1}", d.month,
                    d.strftime("%b"), d.strftime("%Y-%m")])
        d += dt.timedelta(days=1)

days = (end - start).days + 1
rows = []
for order_id in range(1001, 1001 + 420):
    # Slightly more orders in the second year, so year-over-year is positive.
    offset = int(days * random.random() ** 0.85)
    date = start + dt.timedelta(days=min(offset, days - 1))
    customer = random.choice(customers)
    channel = random.choices(["Online", "Store"], weights=[0.6, 0.4])[0]
    for _ in range(random.choice([1, 1, 1, 2, 2, 3])):
        p = random.choices(products, weights=[3, 2, 3, 6, 8, 6, 4, 6, 5])[0]
        qty = random.choice([1, 1, 1, 2]) if p[2] == "Bikes" else random.randint(1, 8)
        discount = random.choice([0, 0, 0, 0.05, 0.1, 0.2])
        rows.append([order_id, date.isoformat(), p[0], customer[0], qty, p[5], discount, channel])
rows.sort(key=lambda r: (r[1], r[0]))

with open(OUT / "sales.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["order_id", "order_date", "product_key", "customer_key", "quantity",
                "unit_price", "discount", "channel"])
    w.writerows(rows)

print(f"wrote {len(rows)} sales rows")
