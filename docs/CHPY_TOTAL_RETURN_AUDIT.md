# CHPY NAV total-return evidence note

Evidence acquired and assessed on 2026-10-17.

## Holdings-date finding

The CSV `Date` is best treated as the publisher's holdings effective date, not automatically as the NAV valuation date.

- The 2026-10-15 and 2026-10-16 CSVs were fetched at 01:30 ET on their reported dates, before a same-day NAV could exist. The CHPY prospectus says NAV is calculated at the scheduled NYSE close, generally 4:00 p.m. ET.
- The official CHPY page displayed NAV `$62.34` "As of 10/15/2026." That independently matches the rounded `$62.3396` NAV embedded in the CSV dated 2026-10-16.
- The CSV dated 2026-10-19 was fetched on Saturday 2026-10-17 at 01:30 ET. October 19 was the next trading weekday, while the embedded NAV therefore corresponds to the preceding Friday, October 16.
- Portfolio rows and fund metadata change across the archived files; the future date is present in the publisher bytes and was not introduced by the collector.
- The CSV dated 2026-10-14 was fetched after the October 14 close. Timing alone cannot establish whether its embedded NAV is for October 13 or October 14, so the audit leaves it unresolved and does not calculate the October 14 ex-date return.

The resulting supported valuation series is:

| Publisher date | Fetch time (UTC) | Inferred valuation date | NAV/share |
| --- | --- | --- | ---: |
| 2026-10-14 | 2026-10-14 23:41:27 | unresolved | $65.7645 |
| 2026-10-15 | 2026-10-15 05:30:04 | 2026-10-14 | $62.9551 |
| 2026-10-16 | 2026-10-16 05:30:06 | 2026-10-15 | $62.3396 |
| 2026-10-19 | 2026-10-17 05:30:04 | 2026-10-16 | $59.9723 |

This inference preserves both original publisher dates and acquisition timestamps. It does not relabel or modify historical rows.

## Distribution evidence

The authoritative source is the [official CHPY fund page](https://yieldmaxetfs.com/our-etfs/chpy/). Its distribution table supplies amount/share, declaration date, ex-date, record date, payable date, and estimated return-of-capital percentage. The page observed on 2026-10-17 reports:

- `$0.5140` per share
- declared 2026-10-13
- ex-date and record date 2026-10-14
- payable 2026-10-15
- estimated ROC 37.84%

The original HTML and metadata are stored immutably under `data/raw/chpy/distributions/`, addressed by SHA-256. Reacquiring identical bytes is idempotent. A changed record is retained as a later observed version; it does not overwrite prior evidence.

Acquisition command:

```sh
PYENV_VERSION=3.12.13 PYTHONPATH=src python -m income_fund_lab.chpy_total_return --acquire
```

## Supported result

The verified consecutive valuation interval is 2026-10-14 through 2026-10-16. The official distribution history confirms no ex-date distribution on October 15 or October 16, so price return and total return are equal over this supported subperiod:

- NAV price return: `-4.7380%`
- compounded NAV total return: `-4.7380%`
- distribution/share in the computable period: `$0.0000`

The October 14 distribution is shown in the audit but is not used without a verified October 13 starting NAV. Return of capital remains included as a distribution whenever an interval is computable.

Run the report with:

```sh
PYENV_VERSION=3.12.13 PYTHONPATH=src python -m income_fund_lab.chpy_total_return --history
```

## Cross-check and limitations

The official site publishes month-end and quarter-end NAV performance, but no authoritative daily total-return series was found for these exact dates. No forced comparison is made. The report flags missing NAVs, incomplete distribution coverage, nonconsecutive valuation dates, explicit corporate actions, and unusually large daily total returns.

Sources:

- [Official CHPY fund page and distribution history](https://yieldmaxetfs.com/our-etfs/chpy/)
- [Official CHPY prospectus](https://yieldmaxetfs.com/wp-content/uploads/funds/CHPY/files/prospectus.pdf), "Determination of Net Asset Value"
- [Official YieldMax distribution schedule](https://yieldmaxetfs.com/distribution-schedule/)
