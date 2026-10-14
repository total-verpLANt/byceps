import re

import pytest

from byceps.services.pizza_delivery import pizza_delivery_service


SERVER_NAME = 'admin-for-pizza-metrics.acmecon.test'
URL = f'http://{SERVER_NAME}/pizza-deliveries/metrics/'

LABEL_NAME_REGEX = re.compile(r'(\w+)="(?:[^"\\]|\\.)*"')


# To be overridden by test parametrization
@pytest.fixture()
def metrics_enabled():
    return False


@pytest.fixture()
def client(metrics_enabled, make_admin_app):
    app = make_admin_app(SERVER_NAME, metrics_enabled=metrics_enabled)
    with app.app_context():
        yield app.test_client()


@pytest.fixture()
def metrics_party(make_party, brand):
    """Provide a party of its own, so counts do not depend on other tests."""
    return make_party(brand)


@pytest.fixture()
def make_entry(metrics_party, user):
    entry_ids = []

    def _wrapper(number, *, delivered=False):
        entry = pizza_delivery_service.create_entry(
            metrics_party.id, number, None, user.id
        ).unwrap()
        entry_ids.append(entry.id)

        if delivered:
            pizza_delivery_service.deliver_entry(entry.id).unwrap()

        return entry

    yield _wrapper

    for entry_id in entry_ids:
        pizza_delivery_service.delete_entry(entry_id)


def _count_line(party_id, status, count):
    return (
        f'pizza_delivery_entries_count{{party="{party_id}", '
        f'status="{status}"}} {count}\n'
    )


@pytest.mark.parametrize('metrics_enabled', [False])
def test_pizza_metrics_disabled_returns_404(client):
    response = client.get(URL)

    assert response.status_code == 404


@pytest.mark.parametrize('metrics_enabled', [True])
def test_pizza_metrics_returns_prometheus_text(client):
    response = client.get(URL)

    assert response.status_code == 200
    assert response.content_type == 'text/plain; version=0.0.4; charset=utf-8'
    assert response.mimetype == 'text/plain'


@pytest.mark.parametrize('metrics_enabled', [True])
def test_pizza_metrics_counts_pending_and_delivered(
    client, metrics_party, make_entry
):
    make_entry('M-1')
    make_entry('M-2')
    make_entry('M-3', delivered=True)

    response = client.get(URL)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert _count_line(metrics_party.id, 'pending', 2) in body
    assert _count_line(metrics_party.id, 'delivered', 1) in body


@pytest.mark.parametrize('metrics_enabled', [True])
def test_pizza_metrics_zero_fills_party_without_entries(client, metrics_party):
    response = client.get(URL)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert _count_line(metrics_party.id, 'pending', 0) in body
    assert _count_line(metrics_party.id, 'delivered', 0) in body


@pytest.mark.parametrize('metrics_enabled', [True])
def test_pizza_metrics_exposes_only_party_and_status_labels(
    client, metrics_party, make_entry
):
    make_entry('M-1')

    response = client.get(URL)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    label_names = set(LABEL_NAME_REGEX.findall(body))
    assert label_names
    assert label_names <= {'party', 'status'}
