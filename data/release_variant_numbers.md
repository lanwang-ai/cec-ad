# Release-variant main numbers

The data in this package are **strongly de-identified** (paths, account names, internal directory names, phone/email-like strings and GitHub account names are masked). Masking changes a few judge decisions, because quoted paths and URLs are checkable evidence spans. On the 179-item test split:

| Judge | Released (de-identified) | Paper (internal text) |
|---|---|---|
| Rule judge (minimal capture) | 54/179 = 30.2% | 31.8% |
| Heuristic | 51/179 = 28.5% | 29.6% |
| Rule v2 | 113/179 = 63.1% | 63.1% |
| Constant-P prior | 81/179 = 45.3% | 45.3% |

The main claim — rule v2 at 63.1% versus 28–32% for the minimal-capture rule judge and the heuristic — is unchanged. Release-variant numbers should be used for any comparison on the released data.
