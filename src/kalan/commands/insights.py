"""Commands for the task 5b insights: ``asgari``, ``aidat``, ``hangikart``, ``paylas``, ``tahmin``,
``erkenkapama``, ``sabitler``, ``disari``. Each calls one :class:`~kalan.service.Kalan` method."""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import Any

from kalan.commands import Out, arg, cols, command, money_for, percent, rate, shorten, table
from kalan.i18n import get_lang, has, t, word
from kalan.insights.export import EXTENSIONS
from kalan.service import Kalan, ServiceError, check_date, check_month

BAR_WIDTH = 24


def _card(item: dict[str, Any]) -> str:
    last4 = item.get("card_last4")
    return f"{item.get('bank', '')} {item.get('product', '')}".strip() + (f" *{last4}" if last4 else "")


def _assumptions(items: list[dict[str, str]]) -> list[str]:
    if not items:
        return []
    lines = ["", t("assumptions")]
    for item in items:
        key = f"assume.{item['key']}"
        text = t(key) if get_lang() == "tr" and has(key) and t(key) else item["text"]
        lines += textwrap.wrap(text, 100, initial_indent="- ", subsequent_indent="  ")
    return lines


# -- asgari -----------------------------------------------------------------------------------------


@command("asgari", "minimum", order=100, help="help.asgari", args=[
    arg("--statement", help="help.statement_id"),
    arg("--with-installments", action="store_true", help="help.asgari.installments"),
    arg("--cycles", type=int, default=12, help="help.asgari.cycles"),
])
def minimum(k: Kalan, a: argparse.Namespace) -> Out:
    r = k.minimum_payment(a.statement, a.with_installments)
    money = money_for(k)
    cur = r["currency"]
    st = k.statement(r["statement_id"])
    only, rates = r["minimum_only"], r["rates"]
    lines = [t("min.head", id=st.id, card=_card(st.model_dump()), balance=money(r["start_balance"], cur))]
    if only["paid_off"]:
        lines.append(t("min.only", cycles=only["cycles"], last=only["last_payment_cut_off"],
                       paid=money(only["total_paid"], cur), cost=money(only["cost"], cur)))
    else:
        lines.append(t("min.not_paid_off", cycles=only["cycles"], left=money(only["balance_left"], cur),
                       paid=money(only["total_paid"], cur), cost=money(only["cost"], cur)))
    lines.append(t("min.full", paid=money(r["pay_in_full"]["total_paid"], cur), extra=money(r["extra_cost"], cur)))
    contractual = rates["contractual_monthly"][0] if rates["contractual_monthly"] else {}
    lines.append(t("min.rates", rate=rate(contractual.get("monthly")),
                   source=word("rate_source", contractual.get("source")),
                   bsmv=percent(rates["bsmv"]["value"]), kkdf=percent(rates["kkdf"]["value"]),
                   ratio=percent(rates["minimum_ratio"]["value"]),
                   ratio_source=word("rate_source", rates["minimum_ratio"]["source"])))
    cycles = r["cycles"] if a.cycles <= 0 else r["cycles"][: a.cycles]
    lines += ["", *table(
        cols("cycle", "cut_off", "balance", "payment", "interest", "taxes", "next_balance"),
        [[c["cycle"], c["cut_off"], money(c["balance"]), money(c["payment"]), money(c["interest"]),
          money(c["bsmv"] + c["kkdf"]), money(c["next_balance"])] for c in cycles],
        right=[0, 2, 3, 4, 5, 6],
    )]
    if len(r["cycles"]) > len(cycles):
        lines.append(t("min.more", count=len(r["cycles"]) - len(cycles)))
    lines += _assumptions(r["assumptions"])
    return Out(r, lines)


# -- aidat ------------------------------------------------------------------------------------------


@command("aidat", "fees", order=110, help="help.aidat", args=[
    arg("--prompt", metavar="TX_ID", help="help.aidat.prompt"),
])
def fees(k: Kalan, a: argparse.Namespace) -> Out:
    money = money_for(k)
    if a.prompt:
        r = k.card_fee_prompt(a.prompt)
        lines = [t("fees.route", route=word("route", r["route"])), "", t("fees.prompt_head"), "",
                 *r["prompt"].rstrip("\n").splitlines(), "", t("fees.note")]
        return Out(r, lines)
    items = k.card_fees()
    rows = [[f["transaction_id"], f["date"], shorten(_card(f), 28), shorten(f["description"], 32),
             money(f["amount"], f["currency"]), t("yes") if f["refunded"] else t("no")] for f in items]
    lines = table(cols("id", "date", "card", "description", "amount", "refunded"), rows, right=[4])
    return Out({"items": items}, [*lines, "", t("fees.hint")] if lines else [t("fees.none")])


# -- hangikart --------------------------------------------------------------------------------------


@command("hangikart", "which-card", order=120, help="help.hangikart", args=[
    arg("--date", help="help.hangikart.date"),
])
def which_card(k: Kalan, a: argparse.Namespace) -> Out:
    r = k.which_card(check_date(a.date))
    cards = r["cards"]
    if not cards:
        return Out(r, [t("card.none")])
    best = r["best"] or cards[0]
    lines = [t("card.best", date=r["date"], card=_card(best), due=best["due_date_effective"],
               days=best["interest_free_days"]), ""]
    lines += table(
        cols("card", "cut_off", "due", "free_days", "first_installment", "basis"),
        [[_card(c) + (" *" if c["stale"] else ""), c["cut_off"], c["due_date_effective"], c["interest_free_days"],
          c["first_installment_month"], word("basis", c["basis"])] for c in cards],
        right=[3],
    )
    if any(c["stale"] for c in cards):
        lines.append(t("card.stale"))
    lines += _assumptions(r["assumptions"])
    return Out(r, lines)


# -- paylas -----------------------------------------------------------------------------------------


@command("paylas", "share", order=130, help="help.paylas", args=[
    arg("--svg", metavar="FILE", help="help.paylas.svg"),
    arg("--amounts", action="store_true", help="help.paylas.amounts"),
])
def share(k: Kalan, a: argparse.Namespace) -> Out:
    r = k.share_card(get_lang(), a.amounts)
    lines = [*textwrap.wrap(r["sentence"], 100), ""]
    lines += table(cols("month", "load"), [[m["month"], "█" * round(float(m["share"]) * BAR_WIDTH)]
                                          for m in r["months"]])
    if a.svg:
        path = Path(a.svg)
        try:
            path.write_text(r["svg"], encoding="utf-8")
        except OSError as exc:
            raise ServiceError("file_unwritable", f"cannot write {path.name} ({exc.strerror or type(exc).__name__})",
                               {"path": str(path)}) from None
        lines += ["", t("share.svg_written", path=str(path))]
    return Out(r, lines)


# -- tahmin -----------------------------------------------------------------------------------------


def _accuracy(bt: dict[str, Any], money) -> str:
    if not bt.get("count") or bt.get("mae") is None:
        return t("forecast.no_accuracy")
    return t("forecast.accuracy", count=bt["count"], mae=money(bt["mae"], bt["currency"]),
             mape=percent(bt["mape"]) if bt.get("mape") is not None else "-",
             bias=money(bt["bias"], bt["currency"]))


@command("tahmin", "forecast", order=140, help="help.tahmin", args=[
    arg("--backtest", action="store_true", help="help.tahmin.backtest"),
    arg("--no-fixed", dest="include_fixed", action="store_false", help="help.tahmin.no_fixed"),
])
def forecast(k: Kalan, a: argparse.Namespace) -> Out:
    money = money_for(k)
    if a.backtest:
        r = k.forecast_backtest(a.include_fixed)
        lines = [_accuracy(r, money)]
        rows = [[m["month"], money(m["forecast"]), money(m["actual"]), money(m["error"])] for m in r["months"]]
        if rows:
            lines += ["", *table(cols("month", "forecast", "actual", "error"), rows, right=[1, 2, 3]),
                      t("amounts_in", currency=money.unit(r["currency"]))]
        return Out(r, lines)
    r = k.forecast(a.include_fixed)
    f, op = r["forecast"], r["open_period"]
    cur = f["currency"]
    parts = f["parts"]
    lines = [
        t("forecast.head", month=f["month"], total=money(f["total"], cur)),
        "",
        *table(cols("part", "amount"), [
            [t("forecast.installments"), money(parts["installments"], cur)],
            [t("forecast.subscriptions"), money(parts["subscriptions"], cur)],
            [t("forecast.variable"), money(parts["variable"], cur)],
            [t("forecast.fixed") + ("" if f["include_fixed"] else f" ({t('forecast.fixed_left_out')})"),
             money(parts["fixed"], cur)],
        ], right=[1]),
        "",
        t("forecast.open", start=op["from"], end=op["to"], count=len(op["transactions"]),
          debit=money(op["debit_total"], cur)),
        _accuracy(r["backtest"], money),
    ]
    return Out(r, lines)


# -- erkenkapama ------------------------------------------------------------------------------------


@command("erkenkapama", "payoff", order=150, help="help.erkenkapama", args=[
    arg("plan_id", nargs="?", help="help.erkenkapama.plan"),
    arg("--month", help="help.erkenkapama.month"),
])
def early_payoff(k: Kalan, a: argparse.Namespace) -> Out:
    money = money_for(k)
    if a.plan_id is None:
        r = k.early_payoff()
        plans = r["plans"]
        if not plans:
            lines = [t("payoff.none")]
        else:
            lines = table(
                cols("id", "plan", "each", "left", "remaining", "without_interest"),
                [[p["plan_id"], shorten(p["description"], 32), money(p["amount_each"]), p["remaining_count"],
                  money(p["remaining_total"]),
                  money(p["remaining_principal"]) if p["remaining_principal"] not in (None, p["remaining_total"])
                  else ""] for p in plans],
                right=[2, 3, 4, 5],
            )
            lines += [t("amounts_in", currency=money.unit(r["currency"])), "", t("payoff.hint")]
        if r["closed_early"]:
            lines += ["", t("payoff.closed", count=len(r["closed_early"]))]
        return Out(r, lines)
    r = k.early_payoff(a.plan_id, check_month(a.month))
    cur = r["currency"]
    lines = [t("payoff.head", description=r["description"], count=r["remaining_count"],
               each=money(r["amount_each"], cur), remaining=money(r["remaining_total"], cur), month=r["payoff_month"])]
    saving = r.get("would_save_if_future_interest_dropped")
    if r.get("remaining_principal") is not None and saving:
        lines.append(t("payoff.principal", principal=money(r["remaining_principal"], cur), saving=money(saving, cur)))
    lines += ["", *table(cols("month", "now", "with_payoff"),
                         [[e["month"], money(e["without"]), money(e["with"])] for e in r["schedule_effect"]],
                         right=[1, 2])]
    lines += ["", *textwrap.wrap(t("payoff.note"), 100)]
    return Out(r, lines)


# -- sabitler ---------------------------------------------------------------------------------------


@command("sabitler", "fixed", order=160, help="help.sabitler")
def fixed(k: Kalan, a: argparse.Namespace) -> Out:
    r = k.fixed_cost_matches()
    money = money_for(k)
    costs = r["costs"]
    if not costs:
        return Out(r, [t("fixed.none")])
    lines = table(
        cols("name", "amount", "day", "matched", "missing"),
        [[shorten(c["name"], 32), money(c["amount"], c["currency"]), c["day"], len(c["matches"]),
          ", ".join(c["missing_months"])] for c in costs],
        right=[1, 2, 3],
    )
    names = {c["fixed_cost_id"]: c["name"] for c in costs}
    for d in r["duplicates"]:
        ids = d["fixed_cost_ids"]
        lines.append(t("fixed.duplicate", a=names.get(ids[0], ids[0]), b=names.get(ids[1], ids[1])))
    tol = r["tolerance"]
    lines += ["", t("fixed.tolerance", amount=percent(tol["amount"]), days=tol["days"])]
    return Out(r, lines)


# -- disari -----------------------------------------------------------------------------------------


@command("disari", "export", order=170, help="help.disari", args=[
    arg("--format", required=True, choices=sorted(EXTENSIONS), help="help.disari.format"),
    arg("--what", choices=["transactions", "plans"], default="transactions", help="help.disari.what"),
    arg("--currency", help="help.disari.currency"),
    arg("--out", metavar="FILE", help="help.disari.out"),
])
def export(k: Kalan, a: argparse.Namespace) -> Out:
    text, skipped = k.export(a.format, a.what, a.currency)
    notes = [t("export.skipped", count=skipped)] if skipped else []
    data: dict[str, Any] = {"format": a.format, "what": a.what, "skipped_rows": skipped}
    if a.out:
        path = Path(a.out)
        try:
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except OSError as exc:
            raise ServiceError("file_unwritable", f"cannot write {path.name} ({exc.strerror or type(exc).__name__})",
                               {"path": str(path)}) from None
        return Out({**data, "path": str(path)},
                   [t("export.written", path=str(path), format=a.format, what=word("what", a.what)), *notes])
    return Out({**data, "text": text}, raw=text, notes=notes)
