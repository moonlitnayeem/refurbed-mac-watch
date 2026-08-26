# Historical price screenshot proofs

This directory stores immutable, viewport-sized screenshots captured when the
watcher records a new all-time low for a hardware comparison key.

Each PNG is captured immediately after the price observation at 1440 × 1200,
large enough to show the top portion of the Refurbed product page: product
imagery, configuration details, and price. The corresponding `state.json`
record stores the screenshot path, observed price, source URL, UTC time, and PNG
SHA-256. Headless Chrome validates the rendered chip, RAM, SSD, and whole-kr
price both before and after screenshot capture. Mismatched or default variants
fail without attaching a screenshot to the historical-low record.

Screenshots are intentionally created **only for new all-time lows**, not every
15-minute observation. This preserves the evidence needed by previous-low links
without growing the repository with duplicate images of unchanged prices.
Historical lows that predate screenshot capture remain unlinked plain text;
their mutable Refurbed URLs are not used as evidence links.
