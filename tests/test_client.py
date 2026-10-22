import pytest

from udc_resource.client import BrokerClient
from udc_resource.errors import BrokerUnavailable


def test_missing_socket_is_reported_as_unavailable(tmp_path):
    client = BrokerClient(str(tmp_path / "missing.sock"), timeout=0.1)
    with pytest.raises(BrokerUnavailable):
        client.request("ping")
