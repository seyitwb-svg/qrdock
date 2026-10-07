"""App entry — product identity lives in product.py, machinery in grkit."""
from grkit.web import make_app
from product import PRODUCT

app = make_app(PRODUCT)
