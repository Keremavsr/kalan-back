"""Shareable installment load card: sentence, shape and a self-contained SVG with no personal data."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from kalan.insights.share_card import load_card
from kalan.models import ManualInstallment
from kalan.schedule import build_schedule


def plans():
    return [
        ManualInstallment(id="pl_a", description="ORNEK MARKET", start_date=date(2099, 1, 3), total_count=3,
                          amount_each=40000),
        ManualInstallment(id="pl_b", description="ORNEK ELEKTRONIK", start_date=date(2099, 1, 4), total_count=12,
                          amount_each=10000),
    ]


def schedule():
    return build_schedule([], plans(), months=12, start_month="2099-01")


def test_sentence_and_shape_tr():
    card = load_card(schedule())
    # January to March 500,00 a month, then 100,00 until December; both plans end inside the window.
    assert card["sentence"] == ("Önümüzdeki 12 ayda taksit yükü en yüksek Ocak 2099 ayında, Nisan 2099 itibarıyla "
                                "yarının altına iniyor; 2 planın 2 tanesi bu sürede bitiyor.")
    assert [m["share"] for m in card["months"][:4]] == ["1.00", "1.00", "1.00", "0.20"]
    assert "total" not in card["months"][0] and "currency" not in card


def test_svg_is_self_contained_and_private():
    card = load_card(schedule(), "en", show_amounts=False)
    svg = card["svg"]
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "ORNEK" not in svg and "pl_a" not in svg
    assert "http" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "@import" not in svg and "<image" not in svg
    assert "500" not in svg  # no amounts by default
    shown = load_card(schedule(), "en", show_amounts=True)
    assert "(500 TRY)" in shown["sentence"] and shown["months"][0]["total"] == 50000
    assert ">500<" in shown["svg"]


def test_empty_schedule():
    card = load_card(build_schedule([], [], months=12, start_month="2099-01"), "en")
    assert card["sentence"] == "No installment load in the next 12 months."
    ET.fromstring(card["svg"])


def test_routes(client, store):
    """With no statement the window starts this month, so the manual plans start today."""
    for p in plans():
        client.post("/transactions", json={"date": date.today().isoformat(), "description": p.description,
                                           "amount": p.amount_each * p.total_count, "currency": "TRY",
                                           "direction": "debit",
                                           "installment": {"total_count": p.total_count,
                                                           "amount_each": p.amount_each}})
    body = client.get("/insights/share-card", params={"locale": "en"}).json()
    assert body["sentence"].startswith("Over the next 12 months")
    r = client.get("/insights/share-card.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "ORNEK" not in r.text
