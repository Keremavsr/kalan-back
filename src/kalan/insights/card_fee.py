"""Annual card fee (kart aidatı): find the fee lines and build a prompt for a refund request letter.

Code finds the facts (fee line, date, card) and the legal basis from ``data/tr_consumer_law.json``.
The letter itself is drafted by the user's own AI from the prompt, either copied by the user or sent
through the selected provider. Personal fields stay placeholders the user fills in afterwards;
kalan never asks for them and never stores them.
"""

from __future__ import annotations

import re
from typing import Any

from kalan.insights.rules import load_json
from kalan.models import Statement, Transaction
from kalan.text import fold

PLACEHOLDERS = ("[AD SOYAD]", "[TC KİMLİK NO]", "[ADRES]", "[TELEFON]", "[E-POSTA]", "[KART SON 4 HANE]", "[TARİH]")
_FEE = re.compile(r"aidat|yillik\s*(?:kart\s*)?(?:uyelik\s*)?ucret|uyelik\s*ucret|kart\s*ucreti|annual\s*fee|"
                  r"membership\s*fee")
_NOT_FEE = re.compile(r"\b(?:bsmv|kkdf)\b|faiz|gecikme")
_SUPPLEMENTARY = re.compile(r"\bek\s*kart")


def consumer_law() -> dict[str, Any]:
    return load_json("tr_consumer_law.json")


def is_card_fee(tx: Transaction) -> bool:
    if tx.kind not in ("fee", "other", "refund") or tx.installment is not None:
        return False
    text = fold(tx.description)
    return bool(_FEE.search(text)) and not _NOT_FEE.search(text)


def find_card_fees(statements: list[Statement]) -> list[dict[str, Any]]:
    """Every annual card fee charged on the stored statements, newest first. A fee is ``refunded`` when
    a credit fee line of the same amount appears on the same card."""
    refunds: dict[tuple[str, str, int], int] = {}
    for st in statements:
        for t in st.transactions:
            if t.direction == "credit" and is_card_fee(t):
                key = (st.bank, st.card_last4 or "", t.amount)
                refunds[key] = refunds.get(key, 0) + 1
    fees: list[dict[str, Any]] = []
    for st in sorted(statements, key=lambda s: (s.period_end, s.id)):
        for t in st.transactions:
            if t.direction != "debit" or not is_card_fee(t):
                continue
            key = (st.bank, st.card_last4 or "", t.amount)
            refunded = refunds.get(key, 0) > 0
            if refunded:
                refunds[key] -= 1
            fees.append({
                "transaction_id": t.id, "statement_id": st.id, "date": t.date.isoformat(),
                "description": t.description, "amount": t.amount, "currency": t.currency, "bank": st.bank,
                "product": st.product, "card_last4": st.card_last4,
                "statement_period_end": st.period_end.isoformat(),
                "supplementary_card": bool(_SUPPLEMENTARY.search(fold(t.description))), "refunded": refunded,
            })
    return sorted(fees, key=lambda f: (f["date"], f["transaction_id"]), reverse=True)


def route_for(amount: int, currency: str) -> str:
    """``hakem_heyeti`` below the current monetary limit, otherwise ``consumer_court``."""
    hh = consumer_law()["hakem_heyeti"]
    if currency != hh["currency"]:
        return "consumer_court"
    return "hakem_heyeti" if amount < hh["limit_below"] else "consumer_court"


def _money(minor: int) -> str:
    whole, frac = divmod(minor, 100)
    return f"{whole:,}".replace(",", ".") + f",{frac:02d}"


def refund_letter_prompt(fee: dict[str, Any]) -> dict[str, Any]:
    """Prompt for the user's AI: a refund request to the bank and, if refused, a Hakem Heyeti application."""
    law = consumer_law()
    hh = law["hakem_heyeti"]
    route = route_for(fee["amount"], fee["currency"])
    basis = "\n".join(f"- {b['title']}: {b['summary']} ({b['source']})" for b in law["legal_basis"])
    if route == "hakem_heyeti":
        venue = (f"the Tüketici Hakem Heyeti (disputes below {_money(hh['limit_below'])} TL in {hh['year']}; "
                 f"apply {hh['apply']})")
    else:
        venue = "the consumer court after mandatory mediation (the amount is not below the Hakem Heyeti limit)"
    supplementary = "yes" if fee["supplementary_card"] else "no"
    prompt = (
        "You are helping a consumer in Turkey ask their bank to refund an annual credit card fee.\n"
        "Write two texts in formal, plain Turkish. Keep them short and polite; no threats, no invented facts.\n\n"
        "Facts (from the user's statement, computed by code):\n"
        f"- Bank: {fee['bank']} ({fee['product']})\n"
        f"- Fee line as printed: \"{fee['description']}\"\n"
        f"- Fee date: {fee['date']}; amount: {_money(fee['amount'])} {fee['currency']}\n"
        f"- Statement cut-off date: {fee['statement_period_end']}\n"
        f"- Supplementary card fee: {supplementary}\n\n"
        f"Legal basis to cite (checked {law['checked_on']}; cite only these, by name):\n{basis}\n\n"
        "Text 1: a letter to the bank's customer complaints unit asking for the refund of this fee to the card "
        "account within 15 days, stating that the consumer wants the card type without an annual fee, and asking "
        "for a written answer.\n"
        f"Text 2: an application to {venue}, to use if the bank refuses or does not answer: parties, subject, "
        "amount, a short account of events, the request, and a list of attachments (the statement page, the "
        "letter to the bank and its answer).\n\n"
        "Use these placeholders exactly as written wherever personal data belongs, and never fill them in: "
        f"{', '.join(PLACEHOLDERS)}.\n"
        'Output only the two texts, under the headings "1. Banka başvurusu" and "2. Hakem Heyeti başvurusu".\n'
    )
    return {
        "fee": fee,
        "route": route,
        "hakem_heyeti": {"year": hh["year"], "limit_below": hh["limit_below"], "currency": hh["currency"],
                         "source": hh["source"], "apply": hh["apply"]},
        "legal_basis": law["legal_basis"],
        "placeholders": list(PLACEHOLDERS),
        "prompt": prompt,
        "note": "kalan does not store the letter or any personal field; fill the placeholders in yourself.",
    }


def placeholders_kept(text: str) -> list[str]:
    """Placeholders still present in a drafted text (a draft that dropped them may contain made-up data)."""
    return [p for p in PLACEHOLDERS if p in text]
