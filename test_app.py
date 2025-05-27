import json
import pytest
from app import app

@pytest.fixture
def client():
    return app.test_client()

def load_examples():
    with open("training_examples.json") as f:
        return json.load(f)

def make_payload(question):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "1234567890",
                        "text": {"body": question}
                    }]
                }
            }]
        }]
    }

def test_webhook_empty(client):
    resp = client.post("/webhook", json={})
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "OK"

@pytest.mark.parametrize("ex", load_examples())
def test_academic_flow(client, ex):
    payload = make_payload(ex["question"])
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "OK"
