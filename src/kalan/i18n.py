"""Message catalogue of the terminal output: English and Turkish, in this one file.

The language is chosen once per run (:func:`set_lang`): ``--lang tr|en``, else ``KALAN_LANG``, else the
operating system's locale (Turkish -> ``tr``, anything else -> ``en``). :func:`t` looks a key up and
fills its ``{placeholders}``; a key missing in Turkish falls back to English, a missing key to itself.
Value words (transaction kinds, categories, statuses ...) live under ``<group>.<value>`` and go through
:func:`word`, which returns the value itself when it has no entry.

JSON output (``--json``, the MCP server, the API) is never translated.
"""

from __future__ import annotations

import ctypes
import locale
import os
import sys
from typing import Literal

Lang = Literal["en", "tr"]
LANGS: tuple[Lang, ...] = ("en", "tr")

_current: Lang = "en"

# key: (English, Turkish). Turkish may be None when it equals the English text.
MESSAGES: dict[str, tuple[str, str | None]] = {
    # -- argparse's own words ---------------------------------------------------------------------
    "argparse.usage: ": ("usage: ", "kullanım: "),
    "argparse.positional arguments": ("positional arguments", "bağımsız değişkenler"),
    "argparse.options": ("options", "seçenekler"),
    "argparse.show this help message and exit": ("show this help message and exit",
                                                 "bu yardımı gösterir ve çıkar"),
    "argparse.%(prog)s: error: %(message)s\n": ("%(prog)s: error: %(message)s\n", "%(prog)s: hata: %(message)s\n"),
    "argparse.the following arguments are required: %s": ("the following arguments are required: %s",
                                                           "şu bağımsız değişkenler gerekli: %s"),
    "argparse.unrecognized arguments: %s": ("unrecognized arguments: %s", "tanınmayan bağımsız değişkenler: %s"),
    "argparse.invalid choice: %(value)r (choose from %(choices)s)": (
        "invalid choice: %(value)r (choose from %(choices)s)", "geçersiz seçim: %(value)r (seçenekler: %(choices)s)"),
    "argparse.invalid %(type)s value: %(value)r": ("invalid %(type)s value: %(value)r",
                                                   "geçersiz %(type)s değeri: %(value)r"),
    "argparse.argument %(argument_name)s: %(message)s": ("argument %(argument_name)s: %(message)s",
                                                         "%(argument_name)s: %(message)s"),
    "argparse.expected one argument": ("expected one argument", "bir değer bekleniyor"),

    # -- global options and built-in commands -----------------------------------------------------
    "cli.description": ("Local, installment-first credit card tracker.",
                        "Taksit odaklı, yerel çalışan kredi kartı takipçisi."),
    "cli.version": ("show the version and exit", "sürümü gösterir ve çıkar"),
    "cli.data_dir": ("folder of the kalan database (default: KALAN_DATA_DIR or the user data folder)",
                     "kalan veritabanının klasörü (varsayılan: KALAN_DATA_DIR ya da kullanıcı veri klasörü)"),
    "cli.lang": ("language of the terminal output: tr or en (default: KALAN_LANG, else the system language)",
                 "terminal çıktısının dili: tr ya da en (varsayılan: KALAN_LANG, yoksa sistem dili)"),
    "cli.json": ("print JSON (money in minor units) instead of a table",
                 "tablo yerine JSON yazar (tutarlar kuruş cinsinden)"),
    "cli.command": ("<command>", "<komut>"),
    "cli.error": ("kalan: {message} ({code})", "kalan: {message} ({code})"),
    "help.serve": ("run the HTTP API on {host}", "HTTP API'yi {host} üzerinde çalıştırır"),
    "help.serve.port": ("port (default {port}; 0 picks a free port)",
                        "port (varsayılan {port}; 0 boş bir port seçer)"),
    "help.serve.print_port": ('print one JSON line {{"event":"ready","port":N}} to stdout when listening',
                              'dinlemeye başlayınca stdout\'a tek satır {{"event":"ready","port":N}} yazar'),
    "help.mcp": ("read-only MCP server on stdio, for Claude Code, Codex or Gemini CLI",
                 "Claude Code, Codex ya da Gemini CLI için stdio üzerinde salt okunur MCP sunucusu"),
    "help.template": ("work with declarative bank templates", "bildirimsel banka şablonlarıyla çalışır"),
    "help.template.check": ("validate a template and run it on one statement",
                            "bir şablonu doğrular ve tek bir ekstrede çalıştırır"),
    "help.template.file": ("template YAML file", "şablon YAML dosyası"),
    "help.template.statement": ("statement PDF (or .txt with the extracted text)",
                                "ekstre PDF'i (ya da çıkarılmış metni içeren .txt)"),
    "help.template.quiet": ("counts and statuses only, no values", "yalnız sayılar ve durumlar, değer yok"),
    "help.template.schema": ("print the template JSON Schema", "şablonun JSON Schema'sını yazar"),

    # -- command help -----------------------------------------------------------------------------
    "help.import": ("import a statement PDF or a CSV, OFX or QIF file",
                    "ekstre PDF'i ya da CSV, OFX veya QIF dosyası içe aktarır"),
    "help.import.file": ("statement PDF (or its extracted .txt), or a .csv, .ofx or .qif export",
                         "ekstre PDF'i (ya da çıkarılmış .txt metni) veya .csv, .ofx, .qif dosyası"),
    "help.import.bank": ("parser or template id (see `kalan banks`); for a CSV, the template whose rules apply",
                         "ayrıştırıcı ya da şablon kimliği (`kalan banks`); CSV'de kuralları uygulanacak şablon"),
    "help.import.no_ai": ("do not send an unrecognised statement to the selected AI provider",
                          "tanınmayan ekstreyi seçili yapay zekâ sağlayıcısına göndermez"),
    "help.statements": ("list stored statements, newest first", "kayıtlı ekstreleri yeniden eskiye listeler"),
    "help.banks": ("parser and template ids for `import --bank`",
                   "`import --bank` için ayrıştırıcı ve şablon kimlikleri"),
    "help.takvim": ("installments already committed for the coming months",
                    "önümüzdeki aylar için şimdiden bağlanmış taksitler"),
    "help.months": ("number of months (default: the months_ahead setting)",
                    "ay sayısı (varsayılan: months_ahead ayarı)"),
    "help.simule": ("what if I buy this in installments?", "bunu taksitle alırsam ne olur?"),
    "help.simule.amount": ("purchase amount, e.g. 9.999,99", "alışveriş tutarı, örneğin 9.999,99"),
    "help.simule.count": ("number of installments", "taksit sayısı"),
    "help.simule.start": ("month of the first installment, YYYY-MM (default: the next statement month)",
                          "ilk taksitin ayı, YYYY-AA (varsayılan: sıradaki ekstre ayı)"),
    "help.simule.interest": ("interest in percent per month, e.g. 3.11 (default: none)",
                             "aylık yüzde faiz, örneğin 3.11 (varsayılan: faizsiz)"),
    "help.simule.months": ("window length in months (default: covers the plan)",
                           "pencere uzunluğu, ay (varsayılan: planı kapsar)"),
    "help.ay": ("spending of one month by category", "bir ayın harcaması, kategoriye göre"),
    "help.ay.month": ("YYYY-MM (default: this month)", "YYYY-AA (varsayılan: bu ay)"),
    "help.abonelikler": ("recurring charges and their price changes", "düzenli ödemeler ve fiyat değişimleri"),
    "help.islemler": ("transactions, newest first; filter by month or text",
                      "işlemler, yeniden eskiye; aya ya da metne göre süzülür"),
    "help.islemler.month": ("YYYY-MM: only the transactions that count in this statement month",
                            "YYYY-AA: yalnız bu ekstre ayına sayılan işlemler"),
    "help.islemler.search": ("text in the description, merchant or category (accents ignored)",
                             "açıklama, işyeri ya da kategoride geçen metin (aksan ve büyük harf fark etmez)"),
    "help.islemler.limit": ("at most this many rows (default 50, 0 for all)",
                            "en çok bu kadar satır (varsayılan 50, hepsi için 0)"),
    "help.uyarilar": ("alerts: limit, due date, interest and fees, missing statements, price rises",
                      "uyarılar: limit, son ödeme, faiz ve ücretler, eksik ekstreler, zamlar"),
    "help.analiz": ("AI commentary on one statement (uses the selected AI provider)",
                    "bir ekstre için yapay zekâ yorumu (seçili sağlayıcıyla)"),
    "help.analiz.id": ("statement id (see `kalan statements`)", "ekstre kimliği (`kalan statements`)"),
    "help.analiz.cached": ("show the stored analysis, do not call the provider",
                           "kayıtlı yorumu gösterir, sağlayıcıyı çağırmaz"),
    "help.statement_id": ("statement id (default: the latest statement)",
                          "ekstre kimliği (varsayılan: en son ekstre)"),
    "help.asgari": ("what paying only the minimum costs, statement by statement",
                    "yalnız asgari ödenirse maliyet, ekstre ekstre"),
    "help.asgari.installments": ("add the installments already scheduled to each month",
                                 "her aya şimdiden planlanmış taksitleri de ekler"),
    "help.asgari.cycles": ("statements shown in the table (default 12, 0 for all)",
                           "tabloda gösterilen ekstre sayısı (varsayılan 12, hepsi için 0)"),
    "help.aidat": ("annual card fee lines, and the prompt for a refund letter",
                   "yıllık kart ücreti satırları ve iade dilekçesi istemi"),
    "help.aidat.prompt": ("print the refund-letter prompt for this fee line (transaction id) to give to your AI",
                          "bu ücret satırı (işlem kimliği) için yapay zekâya verilecek iade dilekçesi istemini yazar"),
    "help.hangikart": ("which card gives the longest interest-free time for a purchase today",
                       "bugünkü alışverişte en uzun faizsiz süreyi hangi kart verir"),
    "help.hangikart.date": ("purchase date YYYY-MM-DD (default: today)",
                            "alışveriş tarihi YYYY-AA-GG (varsayılan: bugün)"),
    "help.paylas": ("shareable installment load card: a sentence and an SVG, no personal data",
                    "paylaşılabilir taksit yükü kartı: bir cümle ve SVG, kişisel veri yok"),
    "help.paylas.svg": ("write the SVG card (1200 x 630) to this file", "SVG kartı (1200 x 630) bu dosyaya yazar"),
    "help.paylas.amounts": ("show rounded amounts (off by default)",
                            "yuvarlanmış tutarları gösterir (varsayılan kapalı)"),
    "help.tahmin": ("forecast of the next statement and the open period so far",
                    "sıradaki ekstrenin tahmini ve şimdiye kadarki açık dönem"),
    "help.tahmin.backtest": ("forecast every past month from earlier data and compare with what came",
                             "her geçmiş ayı önceki verilerle tahmin eder ve gerçekleşenle karşılaştırır"),
    "help.tahmin.no_fixed": ("leave fixed costs out of the total (they may not be paid by card)",
                             "sabit giderleri toplama katmaz (kartla ödenmiyor olabilirler)"),
    "help.erkenkapama": ("early payoff: what is left on every plan, or the effect of closing one",
                         "erken kapama: her planda kalan tutar ya da birini kapatmanın etkisi"),
    "help.erkenkapama.plan": ("plan id (see the list without it)", "plan kimliği (kimliksiz çalıştırınca listelenir)"),
    "help.erkenkapama.month": ("payoff month YYYY-MM (default: the first schedule month)",
                               "kapama ayı YYYY-AA (varsayılan: takvimin ilk ayı)"),
    "help.sabitler": ("fixed costs matched to card rows: months found, months missing, duplicates",
                      "sabit giderlerin kart satırlarıyla eşleşmesi: bulunan ve eksik aylar, mükerrerler"),
    "help.disari": ("export transactions or plans for hledger, beancount, OFX or CSV",
                    "işlemleri ya da planları hledger, beancount, OFX veya CSV olarak dışa aktarır"),
    "help.disari.format": ("file format", "dosya biçimi"),
    "help.disari.what": ("transactions (default) or plans (CSV only)",
                         "işlemler (varsayılan) ya da planlar (yalnız CSV)"),
    "help.disari.currency": ("currency of an OFX file (default: the base currency)",
                             "OFX dosyasının para birimi (varsayılan: ana para birimi)"),
    "help.disari.out": ("write to this file (default: stdout)", "bu dosyaya yazar (varsayılan: stdout)"),

    # -- column headers ---------------------------------------------------------------------------
    "col.id": ("Id", "Kimlik"),
    "col.card": ("Card", "Kart"),
    "col.last4": ("Last4", "Son 4"),
    "col.cut_off": ("Cut-off", "Kesim"),
    "col.due": ("Due", "Son ödeme"),
    "col.total": ("Total", "Toplam"),
    "col.minimum": ("Minimum", "Asgari"),
    "col.check": ("Check", "Kontrol"),
    "col.ai": ("AI", "YZ"),
    "col.month": ("Month", "Ay"),
    "col.installments": ("Installments", "Taksitler"),
    "col.plans": ("Plans", "Plan"),
    "col.fixed": ("Fixed costs", "Sabit giderler"),
    "col.plan": ("Plan", "Plan"),
    "col.source": ("Source", "Kaynak"),
    "col.each": ("Each", "Taksit"),
    "col.left": ("Left", "Kalan"),
    "col.ends": ("Ends", "Bitiş"),
    "col.remaining": ("Remaining", "Kalan tutar"),
    "col.now": ("Now", "Şu an"),
    "col.purchase": ("Purchase", "Alışveriş"),
    "col.with": ("With it", "Alışverişle"),
    "col.category": ("Category", "Kategori"),
    "col.amount": ("Amount", "Tutar"),
    "col.share": ("Share", "Pay"),
    "col.merchant": ("Merchant", "İşyeri"),
    "col.last": ("Last", "Son"),
    "col.before": ("Before", "Önceki"),
    "col.change": ("Change", "Değişim"),
    "col.months": ("Months", "Ay sayısı"),
    "col.last_seen": ("Last seen", "Son görülme"),
    "col.date": ("Date", "Tarih"),
    "col.description": ("Description", "Açıklama"),
    "col.kind": ("Kind", "Tür"),
    "col.severity": ("Severity", "Önem"),
    "col.alert": ("Alert", "Uyarı"),
    "col.cycle": ("No", "No"),
    "col.balance": ("Balance", "Borç"),
    "col.payment": ("Payment", "Ödeme"),
    "col.interest": ("Interest", "Faiz"),
    "col.taxes": ("Taxes", "Vergi"),
    "col.next_balance": ("Next balance", "Sonraki borç"),
    "col.refunded": ("Refunded", "İade"),
    "col.free_days": ("Interest-free days", "Faizsiz gün"),
    "col.first_installment": ("First installment", "İlk taksit"),
    "col.basis": ("Basis", "Dayanak"),
    "col.load": ("Load", "Yük"),
    "col.forecast": ("Forecast", "Tahmin"),
    "col.actual": ("Actual", "Gerçek"),
    "col.error": ("Error", "Hata"),
    "col.without_interest": ("If interest dropped", "Faiz düşerse"),
    "col.with_payoff": ("With payoff", "Kapamayla"),
    "col.name": ("Name", "Ad"),
    "col.day": ("Day", "Gün"),
    "col.matched": ("Matched", "Eşleşen"),
    "col.missing": ("Missing months", "Eksik aylar"),

    # -- value words ------------------------------------------------------------------------------
    "yes": ("yes", "evet"),
    "no": ("no", "hayır"),
    "status.ok": ("ok", "tutuyor"),
    "status.mismatch": ("mismatch", "tutmuyor"),
    "status.none": ("none", "yok"),
    "ai.none": ("none", "yok"),
    "ai.pending": ("pending", "bekliyor"),
    "ai.done": ("done", "var"),
    "ai.failed": ("failed", "başarısız"),
    "source.statement": ("statement", "ekstre"),
    "source.manual": ("manual", "elle"),
    "source.import": ("import", "içe aktarma"),
    "severity.info": ("info", "bilgi"),
    "severity.warning": ("warning", "uyarı"),
    "severity.critical": ("critical", "kritik"),
    "kind.purchase": ("purchase", "alışveriş"),
    "kind.installment": ("installment", "taksit"),
    "kind.cash_advance": ("cash advance", "nakit avans"),
    "kind.fee": ("fee", "ücret"),
    "kind.tax": ("tax", "vergi"),
    "kind.interest": ("interest", "faiz"),
    "kind.payment": ("payment", "ödeme"),
    "kind.refund": ("refund", "iade"),
    "kind.carry_over": ("carried over", "devreden"),
    "kind.other": ("other", "diğer"),
    "category.uncategorized": ("uncategorized", "kategorisiz"),
    "category.groceries": ("groceries", "market"),
    "category.dining": ("dining", "yeme içme"),
    "category.transport": ("transport", "ulaşım"),
    "category.fuel": ("fuel", "yakıt"),
    "category.shopping": ("shopping", "alışveriş"),
    "category.clothing": ("clothing", "giyim"),
    "category.electronics": ("electronics", "elektronik"),
    "category.home": ("home", "ev"),
    "category.utilities": ("utilities", "faturalar"),
    "category.telecom": ("telecom", "iletişim"),
    "category.health": ("health", "sağlık"),
    "category.education": ("education", "eğitim"),
    "category.entertainment": ("entertainment", "eğlence"),
    "category.subscriptions": ("subscriptions", "abonelikler"),
    "category.travel": ("travel", "seyahat"),
    "category.insurance": ("insurance", "sigorta"),
    "category.cash": ("cash", "nakit"),
    "category.fees": ("fees", "ücretler"),
    "category.taxes": ("taxes", "vergiler"),
    "category.transfers": ("transfers", "transferler"),
    "category.other": ("other", "diğer"),
    "basis.printed": ("printed", "ekstrede yazan"),
    "basis.projected": ("projected", "tahmini"),
    "rate_source.override": ("your setting", "kendi ayarınız"),
    "rate_source.statement": ("the statement", "ekstre"),
    "rate_source.rules": ("legal tables", "yasal tablolar"),
    "rate_source.assumption": ("assumption", "varsayım"),
    "route.hakem_heyeti": ("Consumer Arbitration Committee (Tüketici Hakem Heyeti)", "Tüketici Hakem Heyeti"),
    "route.consumer_court": ("consumer court, after mandatory mediation",
                             "zorunlu arabuluculuktan sonra tüketici mahkemesi"),

    # -- existing commands ------------------------------------------------------------------------
    "statements.none": ("No statements yet.", "Henüz ekstre yok."),
    "import.stored": ("Stored statement {id}: {card}", "Ekstre kaydedildi, {id}: {card}"),
    "import.period": ("  period {start} .. {end}, due {due}", "  dönem {start} – {end}, son ödeme {due}"),
    "import.totals": ("  total {total}, minimum {minimum}", "  toplam {total}, asgari {minimum}"),
    "import.rows": ("  {count} transactions, totals check: {status}", "  {count} işlem, toplam kontrolü: {status}"),
    "import.review": (", needs review", ", gözden geçirilmeli"),
    "import.imported": ("Imported {imported} of {rows} rows ({format}, {currency}) as {id}",
                        "{rows} satırdan {imported} tanesi içe aktarıldı ({format}, {currency}), kimlik {id}"),
    "import.skipped": ("  {count} rows skipped as duplicates", "  {count} satır mükerrer olduğu için atlandı"),
    "import.months": ("  months: {months}", "  aylar: {months}"),
    "amounts_in": ("Amounts in {currency}.", "Tutarlar {currency} cinsinden."),
    "simulate.head": ("{amount} in {count} installments from {start} to {end}: {each} a month, total {total}",
                      "{amount}, {count} taksit ({start} – {end}): ayda {each}, toplam {total}"),
    "simulate.interest": (" (interest {interest})", " (faiz {interest})"),
    "simulate.peak": ("Busiest month with it: {month} ({total}).", "Bu alışverişle en yüklü ay: {month} ({total})."),
    "month.head": ("{month}: spent {spend}, credits {credit}", "{month}: harcama {spend}, alacak {credit}"),
    "month.previous": (", previous month {spend} ({change})", ", önceki ay {spend} ({change})"),
    "subscriptions.none": ("No recurring charges found.", "Düzenli ödeme bulunamadı."),
    "transactions.none": ("No transactions match.", "Eşleşen işlem yok."),
    "alerts.none": ("No alerts.", "Uyarı yok."),
    "analyze.no_summary": ("(no summary)", "(özet yok)"),
    "analyze.footer": ("Provider: {provider}, {created}. Categories were applied.",
                       "Sağlayıcı: {provider}, {created}. Kategoriler işlemlere uygulandı."),

    # -- alerts (one line per kind; params are formatted before they get here) ---------------------
    "alert.installment_ending": ("last installment of {description} ({amount_each})",
                                 "{description} için son taksit ({amount_each})"),
    "alert.limit_high": ("{used_share} of the limit used ({statement_total} of {credit_limit})",
                         "limitin {used_share} kadarı kullanılmış ({credit_limit} limitten {statement_total})"),
    "alert.interest_charged": ("interest charged: {count} rows, {total}", "faiz işlemiş: {count} satır, {total}"),
    "alert.fee_charged": ("fees charged: {count} rows, {total}", "ücret alınmış: {count} satır, {total}"),
    "alert.tax_charged": ("taxes charged: {count} rows, {total}", "vergi alınmış: {count} satır, {total}"),
    "alert.foreign_currency": ("{count} foreign currency rows ({currencies}), {total}",
                               "{count} döviz işlemi ({currencies}), {total}"),
    "alert.due_soon": ("payment due {due_date} ({days_left} days): {statement_total}, minimum {minimum_payment}",
                       "son ödeme {due_date} ({days_left} gün): {statement_total}, asgari {minimum_payment}"),
    "alert.missing_statement": ("no statement for {card}", "{card} için ekstre eksik"),
    "alert.subscription_price_rise": ("{merchant} went from {amount_previous} to {amount_last} ({price_change})",
                                      "{merchant} {amount_previous} tutarından {amount_last} tutarına çıktı "
                                      "({price_change})"),
    "alert.statement_overdue": ("next statement of {card} expected {expected_cut_off}, {days_overdue} days late",
                                "{card} için sıradaki ekstre {expected_cut_off} tarihinde bekleniyordu, "
                                "{days_overdue} gün gecikti"),
    "alert.charged_after_cancel": ("{merchant} charged {count} times ({total}) after the cancellation on "
                                   "{cancelled_on}",
                                   "{merchant}, {cancelled_on} tarihindeki iptalden sonra {count} kez çekim yaptı "
                                   "({total})"),

    # -- asgari -----------------------------------------------------------------------------------
    "min.head": ("Statement {id} ({card}): balance {balance}.", "{id} ekstresi ({card}): borç {balance}."),
    "min.only": ("Paying only the minimum: {cycles} statements, last payment on {last}, total paid {paid}, "
                 "of which interest and taxes {cost}.",
                 "Yalnız asgari ödenirse: {cycles} ekstre, son ödeme {last}, toplam ödeme {paid}; bunun {cost} "
                 "tutarı faiz ve vergi."),
    "min.not_paid_off": ("Paying only the minimum: still {left} owed after {cycles} statements; paid {paid}, "
                         "interest and taxes {cost}.",
                         "Yalnız asgari ödenirse {cycles} ekstreden sonra hâlâ {left} borç kalıyor; ödenen "
                         "{paid}, faiz ve vergi {cost}."),
    "min.full": ("Paying in full now: {paid}. Paying only the minimum costs {extra} more.",
                 "Şimdi tamamı ödenirse: {paid}. Yalnız asgari ödemek {extra} daha pahalıya geliyor."),
    "min.rates": ("Rates: contractual interest {rate} a month ({source}), BSMV {bsmv} and KKDF {kkdf} of the "
                  "interest, minimum payment {ratio} of the balance ({ratio_source}).",
                  "Oranlar: aylık akdi faiz {rate} ({source}), faizin {bsmv} BSMV ve {kkdf} KKDF, asgari ödeme "
                  "borcun {ratio} kadarı ({ratio_source})."),
    "min.more": ("{count} more statements; --cycles 0 shows all of them.",
                 "{count} ekstre daha var; hepsi için --cycles 0."),
    "assumptions": ("Assumptions:", "Varsayımlar:"),
    "assume.minimum_paid_on_time": ("The minimum is paid in full by every due date, so no late interest.",
                                    "Asgari tutar her son ödeme gününde tam ödenir, gecikme faizi oluşmaz."),
    "assume.interest_on_unpaid_only": (
        "Interest runs on the unpaid part for the real days between two cut-off dates, divided by 30, without "
        "compounding inside a cycle.",
        "Faiz, ödenmeyen kısma iki kesim tarihi arasındaki gerçek gün sayısı kadar işler (gün / 30); dönem içinde "
        "faize faiz yürümez."),
    "assume.rates_follow_caps": (
        "Rates follow the statement's printed rate, capped by the TCMB maximum in force on each cut-off date; "
        "future cap changes are not known.",
        "Oran, ekstrede yazan orandır ve her kesim tarihinde geçerli TCMB azamisiyle sınırlanır; ileride azaminin "
        "nasıl değişeceği bilinmiyor."),
    "assume.scheduled_installments_added": (
        "Installments already scheduled are added to each month's statement; nothing else is spent.",
        "Şimdiden planlanmış taksitler her ayın ekstresine eklenir; başka harcama yapılmaz."),
    "assume.no_new_spending": ("The card is not used again until the debt is gone.",
                               "Borç bitene kadar kart yeniden kullanılmaz."),
    "assume.cut_off_day_kept": ("Later cut-off dates keep this statement's day of the month (clipped in short "
                                "months).",
                                "Sonraki kesim tarihleri bu ekstrenin ayın kaçıncı günü olduğunu korur (kısa aylarda "
                                "ayın son gününe çekilir)."),
    "assume.cut_off_day_included": (
        "A purchase on the cut-off day is billed on that statement; a late posting by the merchant may move it to "
        "the next one.",
        "Kesim günü yapılan alışveriş o ekstreye girer; işyeri geç yansıtırsa bir sonrakine kayabilir."),
    "assume.day_and_gap_kept": (
        "Later cut-offs keep the last statement's day of the month and the same number of days to the due date, "
        "unless the statement prints the next dates.",
        "Ekstre sonraki tarihleri yazmıyorsa, sonraki kesimler son ekstrenin gününü ve son ödemeye kalan gün sayısını "
        "korur."),
    "assume.due_on_next_business_day": (
        "A due date on a weekend or public holiday is paid on the next business day without interest.",
        "Hafta sonuna ya da resmî tatile denk gelen son ödeme, faizsiz olarak ilk iş gününe kayar."),

    "assume.holidays_unknown": ("", "Bazı yıllar için dinî bayramlar veride yok; o yıllarda yalnız hafta sonları ve "
                                    "sabit tatiller hesaba katıldı."),
    "assume.stale_statement": ("", "Bazı kartların son haftalarda ekstresi yok; bu kartların tarihleri tahmini."),

    # -- aidat ------------------------------------------------------------------------------------
    "fees.none": ("No annual card fee lines found.", "Yıllık kart ücreti satırı bulunamadı."),
    "fees.hint": ("Prompt for a refund letter: kalan aidat --prompt <Id>",
                  "İade dilekçesi istemi için: kalan aidat --prompt <Kimlik>"),
    "fees.route": ("If the bank refuses, apply to: {route}.", "Banka reddederse başvuru yeri: {route}."),
    "fees.prompt_head": ("Prompt for your AI (copy everything below):",
                         "Yapay zekâya verilecek istem (aşağıdakinin tamamı kopyalanmalı):"),
    "fees.note": ("kalan stores neither the letter nor any personal field; fill in the placeholders yourself.",
                  "kalan dilekçeyi ve kişisel bilgileri saklamaz; köşeli parantezli alanlar elle doldurulmalı."),

    # -- hangikart --------------------------------------------------------------------------------
    "card.best": ("Best card for an installment purchase on {date}: {card}, first payment {due} "
                  "({days} days without interest).",
                  "{date} tarihli taksitli alışveriş için en uygun kart: {card}; ilk ödeme {due} "
                  "({days} gün faizsiz)."),
    "card.none": ("No cards yet; import a statement first.", "Henüz kart yok; önce bir ekstre içe aktarılmalı."),
    "card.stale": ("* no statement in the last weeks: dates are projected.",
                   "* son haftalarda ekstresi yok: tarihler tahmini."),

    # -- paylas -----------------------------------------------------------------------------------
    "share.svg_written": ("SVG card written to {path}.", "SVG kart kaydedildi: {path}."),

    # -- tahmin -----------------------------------------------------------------------------------
    "forecast.head": ("Forecast for the {month} statement: {total}", "{month} ekstresi tahmini: {total}"),
    "forecast.installments": ("installments already scheduled", "planlanmış taksitler"),
    "forecast.subscriptions": ("subscriptions", "abonelikler"),
    "forecast.variable": ("other spending (median)", "diğer harcama (medyan)"),
    "forecast.fixed": ("fixed costs", "sabit giderler"),
    "forecast.fixed_left_out": ("not in the total", "toplama dahil değil"),
    "col.part": ("Part", "Kalem"),
    "forecast.open": ("Open period {start} – {end}: {count} rows, {debit} spent so far.",
                      "Açık dönem {start} – {end}: {count} işlem, şimdiye kadar {debit} harcama."),
    "forecast.accuracy": ("Accuracy over {count} past months: mean error {mae} ({mape}), bias {bias}.",
                          "Geçmiş {count} ayda isabet: ortalama hata {mae} ({mape}), sapma {bias}."),
    "forecast.no_accuracy": ("Not enough history yet to measure the accuracy.",
                             "İsabeti ölçmek için henüz yeterli geçmiş yok."),

    # -- erkenkapama ------------------------------------------------------------------------------
    "payoff.none": ("No open installment plans.", "Açık taksit planı yok."),
    "payoff.head": ("{description}: instead of {count} installments of {each}, pay {remaining} in {month}.",
                    "{description}: kalan {count} taksit ({each}) yerine {month} ayında {remaining} ödenir."),
    "payoff.principal": ("If the bank drops the future interest and taxes: {principal} ({saving} less).",
                         "Banka ileriki faiz ve vergileri düşerse: {principal} ({saving} daha az)."),
    "payoff.note": ("kalan does not know this bank's early payoff rule. Ask the bank for the payoff amount; it is at "
                    "most the remaining total shown here.",
                    "kalan bu bankanın erken kapama kuralını bilmiyor. Kapama tutarı bankaya sorulmalı; buradaki "
                    "kalan tutardan fazla olmaz."),
    "payoff.closed": ("Plans the statements already show as closed early: {count}.",
                      "Ekstrelerde erken kapandığı görülen plan: {count}."),
    "payoff.hint": ("Effect of closing one plan: kalan erkenkapama <Id> [--month YYYY-MM]",
                    "Bir planı kapatmanın etkisi için: kalan erkenkapama <Kimlik> [--month YYYY-AA]"),

    # -- sabitler ---------------------------------------------------------------------------------
    "fixed.none": ("No fixed costs yet.", "Henüz sabit gider yok."),
    "fixed.tolerance": ("A card row matches when its amount is within {amount} and its date within {days} days.",
                        "Kart satırı, tutarı {amount} ve tarihi {days} gün içindeyse eşleşir."),
    "fixed.duplicate": ("Possible duplicate: {a} and {b}.", "Mükerrer olabilir: {a} ve {b}."),

    # -- disari -----------------------------------------------------------------------------------
    "export.written": ("Wrote {path} ({format}, {what}).", "{path} yazıldı ({format}, {what})."),
    "export.skipped": ("{count} rows in other currencies were left out.",
                       "Başka para birimindeki {count} satır dışarıda kaldı."),
    "what.transactions": ("transactions", "işlemler"),
    "what.plans": ("plans", "planlar"),

    # -- errors (by code; the English text comes from the code that raised it) ---------------------
    "error.file_unreadable": ("", "Dosya okunamadı."),
    "error.file_too_large": ("", "Dosya 10 MB'tan büyük."),
    "error.unknown_bank": ("", "Bu kimlikte ayrıştırıcı ya da şablon yok (`kalan banks`)."),
    "error.not_pdf": ("", "Dosya PDF değil."),
    "error.invalid_pdf": ("", "PDF açılamadı."),
    "error.unknown_format": ("", "Desteklenen bir ekstre biçimi tanınmadı."),
    "error.parse_error": ("", "Ekstre tanındı ama okunamadı."),
    "error.duplicate": ("", "Bu ekstre zaten kayıtlı."),
    "error.ai_fallback_failed": ("", "Yapay zekâ ekstreyi okuyamadı."),
    "error.import_unreadable": ("", "Dosya içe aktarılamadı."),
    "error.statement_not_found": ("", "Bu kimlikte ekstre yok."),
    "error.no_statements": ("", "Önce bir ekstre içe aktarılmalı."),
    "error.no_provider": ("", "Önce bir yapay zekâ sağlayıcısı seçilmeli (uygulamada)."),
    "error.provider_unavailable": ("", "Seçili yapay zekâ sağlayıcısı bu bilgisayarda kullanılamıyor."),
    "error.analysis_failed": ("", "Yapay zekâ yorumu tamamlanamadı."),
    "error.analysis_not_found": ("", "Bu ekstre henüz yorumlanmadı."),
    "error.invalid_month": ("", "Ay YYYY-AA biçiminde olmalı."),
    "error.invalid_date": ("", "Tarih YYYY-AA-GG biçiminde olmalı."),
    "error.invalid_months": ("", "--months 1 ile 60 arasında olmalı."),
    "error.invalid_amount": ("", "Tutar okunamadı."),
    "error.invalid_simulation": ("", "Benzetim değerleri geçersiz."),
    "error.card_fee_not_found": ("", "Bu kimlikte yıllık kart ücreti satırı yok."),
    "error.plan_not_found": ("", "Bu kimlikte açık taksit planı yok."),
    "error.month_outside_schedule": ("", "Kapama ayı takvim penceresinin içinde olmalı."),
    "error.plans_csv_only": ("", "Planlar yalnız CSV olarak dışa aktarılır; hledger ve beancount onları zaten içerir."),
    "error.file_unwritable": ("", "Dosya yazılamadı."),
}


def _os_lang() -> Lang:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return "tr" if value.lower().startswith("tr") else "en"
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, len(buf)):  # type: ignore[attr-defined]
                return "tr" if buf.value.lower().startswith("tr") else "en"
        except (AttributeError, OSError):
            pass
    name = (locale.getlocale()[0] or "").lower()
    return "tr" if name.startswith(("tr", "turkish")) else "en"


def resolve_lang(explicit: str | None = None) -> Lang:
    """``--lang`` value, else ``KALAN_LANG``, else the OS locale."""
    for value in (explicit, os.environ.get("KALAN_LANG")):
        if value:
            value = value.lower()[:2]
            if value in LANGS:
                return value  # type: ignore[return-value]
    return _os_lang()


def set_lang(lang: Lang) -> None:
    global _current
    _current = lang


def get_lang() -> Lang:
    return _current


def has(key: str) -> bool:
    return key in MESSAGES


def t(key: str, **params: object) -> str:
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    en, tr = entry
    text = tr if _current == "tr" and tr is not None else en
    return text.format(**params)


def word(group: str, value: str | None) -> str:
    """Translated value word (``word("kind", "fee")`` -> ``"ücret"``), or the value itself."""
    if value is None:
        return ""
    key = f"{group}.{value}"
    return t(key) if key in MESSAGES else value


def argparse_gettext(text: str) -> str:
    """Stand-in for argparse's gettext: its own few words in the current language."""
    key = f"argparse.{text}"
    return t(key) if key in MESSAGES else text
