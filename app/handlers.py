"""Implementations called by the generated, typed HTTP adapters."""

from app import auth, orders, products, promos


def register(request, body):
    return auth.register(body)


def login(request, body):
    return auth.login(body)


def refresh(request, body):
    return auth.refresh(body)


def create_product(request, body, user):
    return products.create(body, user)


def get_product(request, id, user):
    return products.get(id)


def list_products(request, page, size, status, category, user):
    return products.list_all(page, size, status, category)


def update_product(request, id, body, user):
    return products.update(id, body, user)


def delete_product(request, id, user):
    products.archive(id, user)


def create_order(request, body, user):
    return orders.create(body, user)


def get_order(request, id, user):
    return orders.get(id, user)


def update_order(request, id, body, user):
    return orders.update(id, body, user)


def cancel_order(request, id, user):
    return orders.cancel(id, user)


def transition_order(request, id, body, user):
    return orders.transition(id, body, user)


def create_promo(request, body, user):
    return promos.create(body, user)
