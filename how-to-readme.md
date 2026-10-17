# How to upload actual lodging revenue

Open **Resultater → Importér afsluttet dagsresultat** in the app.

## 1. Export the reports from Picasso

Use the same **completed stay date** for both reports, after the night run.

| Report | Settings | Used for |
|---|---|---|
| **Hotelmanager PDF** | Include parts 1 and 2 (Room/Bed). Exclude VAT, packages, credit and out-of-order, using the agreed report settings. | Final room and bed counts, plus a rounded revenue cross-check. |
| **Product Turnover PDF** | Reports (F7) → Statistik → Produkter. Select Overnight, product names and grouping by room type. Exclude VAT. Include Confirmed, Guaranteed, In-House and Out of House; exclude other statuses. | Detailed amounts by room type and identification of breakfast deductions. |

**Check the date basis.** A Product Turnover footer saying “Arrival date” does
not by itself establish revenue for each stay night. Confirm the selection and
allocation of multi-night stays before accepting the amounts as actual lodging
revenue for that stay date.

Product names such as YP do not identify whether a sale is a whole room or an
individual bed. The configured room-type code determines that split.

## 2. Upload and review

1. Upload the Hotelmanager and Product Turnover PDFs together.
2. Review the extracted room and bed counts, amounts by room type, breakfast
   deductions and differences between the reports.
3. Confirm final room and bed counts separately from revenue. Hotelmanager can
   also be uploaded alone if only the counts are ready.
4. Accept revenue only after confirming the stay date, completeness, VAT basis
   and breakfast deductions, and resolving the differences shown on the page.

Uploading creates a draft for review. It does not automatically confirm actual
results. If the revenue checkbox is disabled, resolve the listed issues first;
confirming the date alone does not override an unresolved reconciliation.

## 3. If the amounts do not match

Add the **Accounts PDF** for the same date as an optional third file. This
compares product groups and shows postings without a product link under “None”.

If a difference remains, export **Accounts → All transactions** for the relevant
account:

- **1002:** lodging, including manual adjustments, refunds and discounts.
- **1004:** upgrades.
- **1005:** breakfast, when its allocation needs investigation.

Set the account in **Account numbers**, leave **Exclude** off, and keep the
product filter unrestricted so “None” postings remain visible. Export each
relevant account to PDF for investigation. Transaction-detail PDFs are
supporting evidence; the current upload field for Accounts expects the summary
report, not the All transactions layout.

Check bill, folio and reservation references to connect adjustments to their
underlying postings. A booking/accounting date is not necessarily the stay date.
Do not assign unlinked amounts to a room type without evidence, or count the
Accounts amounts again on top of Product Turnover.

An MM package can include breakfast even when its line has room or guest counts.
Use the accounting detail or underlying reservation to verify the deduction;
do not assume every breakfast amount appears on a separate breakfast-only line.

## 4. What revenue should be compared with the model?

Use **actual lodging revenue after any discounts actually posted, excluding VAT
and breakfast**. Do not assume a discount on future dates. Refunds, corrections
and upgrades need their stay-date treatment checked separately.

Model prices are **inclusive of 25% VAT and exclude breakfast**. Convert a
comparable model revenue forecast to excluding VAT by dividing by **1.25**.
The app's forecast-validation view handles this only where the recorded revenue
basis permits it; the pricing dashboard's revenue figures are not automatically
the same comparison basis.

For 16 October 2026, the reconciliation was:

- Product-linked lodging after breakfast: **28,506.89 kr.**
- Actual posted discount: **100.00 kr.**
- Reconciled lodging total: **28,406.89 kr.**, rounding to Hotelmanager's
  **28,407 kr.**

This reconciles the rounded total. It does not by itself confirm the full
room-type allocation, the treatment of other adjustments or the date basis.
Product Turnover's printed Grand Total has a separate one-øre rounding
difference from the summed accounting product groups.

## 5. Revenue accuracy versus profitability

Comparing forecast revenue with actual revenue measures **forecast accuracy**.
It does not establish that the model earns more than the normal pricing
strategy; use the planned A/B comparison for that question.

To calculate actual **contribution**, also enter the actual commission and
variable costs for the same stay date and on a consistent net basis. Unknown
costs remain missing; enter zero only when the actual cost is zero.
