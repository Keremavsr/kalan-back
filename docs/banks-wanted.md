# Banks wanted

kalan ships a bank only when it has a synthetic sample that is faithful to the real statement: the same
line structure in PyMuPDF `get_text()` output, the same labels, number and date formats, and the same
summary block, so that the ground-truth tests mean something. Public documentation (help pages, sample
images in marketing material) does not show that structure, so none of the banks below is shipped yet.

Today kalan reads them in two other ways:

- the **AI fallback reader** (any provider you select), which flags a statement for review when it does
  not reconcile;
- **CSV, OFX or QIF exports** from the bank's website or app (`POST /imports`).

## How to help

If you hold a card from one of these banks:

1. Upload one statement in kalan. If the AI fallback reads it, ask for a template draft
   (`POST /statements/{id}/template-draft`), or write the template yourself (see
   [adding-a-bank.md](adding-a-bank.md)).
2. Run `kalan template check <template.yaml> <statement.pdf>` until it reports `reconciliation: ok` on
   every statement you have.
3. Build a synthetic sample with invented merchants, amounts, card number and name (see
   `tools/synth_vakifbank.py`) and open a pull request with the template and the sample. Paste the
   output of `kalan template check ... --quiet`, which contains no values. Never attach a real statement.

## The list

| Country | Bank | Products | Notes |
| --- | --- | --- | --- |
| Turkey | Garanti BBVA | Bonus | TRY, installments printed per row |
| Turkey | Yapı Kredi | World | TRY |
| Turkey | İş Bankası | Maximum | TRY |
| Turkey | Akbank | Axess, Wings | TRY |
| Turkey | QNB | CardFinans | TRY |
| Turkey | Ziraat Bankası | Bankkart | TRY |
| Turkey | Enpara | Enpara.com kredi kartı | TRY |
| Brazil | Nubank | Mastercard | BRL, *parcela k/n*; CSV export exists |
| Brazil | Itaú | | BRL, *parcelado* |
| Brazil | Bradesco | | BRL |
| Brazil | Inter | | BRL |
| Brazil | C6 Bank | | BRL |
| Mexico | BBVA México | | MXN, *meses sin intereses* |
| Mexico | Banorte | | MXN, *meses sin intereses* |
| Argentina | Galicia | | ARS, *cuotas* |
| Argentina | Santander | | ARS, *cuotas* |
| Israel | Isracard | | ILS, *tashlumim* |
| Israel | Cal | | ILS |
| Israel | Max | | ILS |

Shipped today: VakıfBank Worldcard (Turkey), as the template `src/kalan/templates/tr_vakifbank_worldcard.yaml`.
