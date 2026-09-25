# -*- coding: utf-8 -*-
"""FAQ provenance policy — 2026-09-26.

HARD RULE (user decision, 2026-09-26):
    "Frequently Asked Questions 主要就是根据 LCSC 的数据源，没有的我们自己不生成"
    A product-page FAQ may ONLY exist if it originates from the LCSC RAW record
    for that very part (`source_raw.main_product.faqs`). Any FAQ text that is
    computed / templated / rewritten by our own code is fabrication and must
    never reach a page, a MASTER row or a release plan.

That rule kills three things at once:
  * 02-cleaning template factories (`tools/factory/category.py::_faq`, reused by
    `lcsc_http_adapter.py`) that baked `Q: ...?A: ...` sentences into MASTER.faq.
  * `gen_parts.py` "Pass B", a MASTER.faq fallback that re-opened that hole on
    2026-09-22 and rendered the fabricated sentences to ~8 pages.
  * any future well-meaning re-enablement: `release_pipeline.check_faq_
    provenance()` stops the batch with FAQ_FABRICATED_FAIL before staging.

Only ONE escape hatch exists and it is deliberately narrow:
`VERIFIED_MPNS` below. A part may keep a non-source FAQ only when a human has
personally verified the sentence against the datasheet / RAW attributes AND
recorded who and when. It is opt-in per MPN, it is auditable, and it is the
only place where the failure-closed gate is told to stay shut.
"""

# mpn-upper -> {"reason": ..., "approved_by": ..., "date": ...}
VERIFIED_MPNS = {
    "AO3400A": {
        "reason": ("'30.0 V DS' matches RAW attributes_json Vds=30 V for this part; "
                   "human-approved as correct on 2026-09-26."),
        "approved_by": "user",
        "date": "2026-09-26",
    },
}


def is_verified(mpn):
    """True when this MPN carries a human-verified, non-source FAQ."""
    if not mpn:
        return False
    return mpn.strip().upper() in VERIFIED_MPNS


def verified_mpns():
    """Upper-cased MPN set allowed to render a non-source FAQ."""
    return {m.strip().upper() for m in VERIFIED_MPNS}


#: Stop code used by the release gate.
FAQ_FABRICATED_FAIL = "FAQ_FABRICATED_FAIL"
