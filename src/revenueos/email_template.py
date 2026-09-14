"""The findings email: what a business receives after asking to see the list.

This is the only outbound message that should look designed. The first touch stays plain, because a
branded template on a cold open reads as a campaign and gets deleted; the repo's own
gtm/cold-email-copywriter skill treats that as an auto-reject. By the time someone has replied
asking for the list they have opted in, and the job changes from earning a reply to being worth
their time.

Email constraints shape every structural decision. No <style> block, because Gmail strips it. No
flexbox or grid, because Outlook renders through Word's engine. Layout is tables with inline
styles, the button is a table cell rather than a padded anchor so it stays clickable in Outlook,
and the preheader is hidden text that controls the inbox preview line.

Two craft rules the first draft broke and this one holds. Type sizes come from a scale rather than
being picked per element, with tracking tied to size: tight on the headline because letters read
too far apart as they grow, near zero on body, slightly positive on the small print. Spacing comes
from a four-pixel scale, so every gap is a multiple and none of it is improvised.

Evidence is set in monospace on purpose. It is literal machine output, pixel IDs and tag IDs the
reader can search their own page for, and monospace says that honestly rather than decorating it.

Every claim rendered here must have been verified against the live page first. The crawler once
produced findings about phone numbers on pages that display none, and a designed email makes a
false claim worse, not better.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

# Palette, from the product site's own stylesheet.
INK, SOFT, MUTED = "#0f172a", "#44506b", "#6b7280"
ACCENT, ACCENT_SOFT = "#3d3ff0", "#eef0ff"
BORDER, PAGE, PANEL, WELL = "#e6e7eb", "#f4f4f2", "#ffffff", "#f8f8f7"

MARK = "https://revenueos.com.au/press/revenueos-mark-accent-320.png"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

# Type scale. Tracking is a function of size, never one value reused.
H1 = f"font-size:24px;line-height:1.2;letter-spacing:-.52px;font-weight:700;color:{INK};"
LEAD = f"font-size:15px;line-height:1.6;letter-spacing:-.08px;color:{SOFT};"
ITEM = f"font-size:15px;line-height:1.4;letter-spacing:-.16px;font-weight:600;color:{INK};"
BODY = f"font-size:14px;line-height:1.55;letter-spacing:-.05px;color:{SOFT};"
CODE = f"font-family:{MONO};font-size:12px;line-height:1.5;letter-spacing:0;color:{MUTED};"
NOTE = f"font-size:13px;line-height:1.6;letter-spacing:-.02px;color:{MUTED};"
FINE = f"font-size:12px;line-height:1.65;letter-spacing:.08px;color:{MUTED};"
NUM = f"font-family:{MONO};font-size:11px;line-height:1;letter-spacing:.4px;color:{ACCENT};font-weight:600;"


@dataclass(frozen=True)
class Finding:
    """One verified problem on the customer's site.

    `evidence` is the literal observation, so the reader can check it rather than trust us.
    `cost` says why it matters in their terms, not ours.
    """
    title: str
    evidence: str
    cost: str


def _row(f: Finding, i: int, last: bool) -> str:
    rule = "" if last else f"border-bottom:1px solid {BORDER};"
    return f"""
                <tr>
                  <td style="padding:20px 0;{rule}">
                    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
                      <tr>
                        <td width="28" style="width:28px;vertical-align:top;padding-top:3px;">
                          <span style="{NUM}">{i:02d}</span>
                        </td>
                        <td style="vertical-align:top;">
                          <div style="{ITEM}">{escape(f.title)}</div>
                          <div style="{BODY}padding-top:6px;">{escape(f.cost)}</div>
                          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-top:12px;">
                            <tr>
                              <td style="background:{WELL};border:1px solid {BORDER};border-radius:7px;padding:10px 12px;">
                                <div style="{CODE}">{escape(f.evidence)}</div>
                              </td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>"""


def findings_email(*, business: str, page: str, findings: list[Finding],
                   reply_to: str = "hello@revenueos.com.au") -> str:
    """Render the complete HTML email."""
    rows = "".join(_row(f, i + 1, i == len(findings) - 1) for i, f in enumerate(findings))
    n = len(findings)
    noun = "one thing" if n == 1 else f"{n} things"
    preheader = f"{noun} on {page}, each with what we actually saw on the page."

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light only">
<title>What we found on {escape(page)}</title></head>
<body style="margin:0;padding:0;background:{PAGE};-webkit-text-size-adjust:100%;">
<div style="display:none;font-size:1px;color:{PAGE};line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">{escape(preheader)}</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;background:{PAGE};">
  <tr>
    <td align="center" style="padding:32px 16px 44px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="580" style="border-collapse:collapse;width:580px;max-width:100%;">

        <tr>
          <td style="padding:0 0 20px 2px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
              <td width="26" style="width:26px;padding-right:10px;">
                <img src="{MARK}" width="26" height="26" alt="RevenueOS" style="display:block;width:26px;height:26px;border:0;">
              </td>
              <td style="font-family:{SANS};font-size:14px;font-weight:700;letter-spacing:-.2px;color:{INK};">RevenueOS</td>
            </tr></table>
          </td>
        </tr>

        <tr>
          <td style="background:{PANEL};border:1px solid {BORDER};border-radius:16px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
              <tr><td style="height:4px;background:{ACCENT};border-radius:15px 15px 0 0;font-size:0;line-height:0;">&nbsp;</td></tr>
              <tr>
                <td style="padding:32px 32px 28px;font-family:{SANS};">

                  <div style="{H1}">What we found on {escape(page)}</div>
                  <div style="{LEAD}padding-top:12px;">
                    You asked for the list, {escape(business)}. Here is all of it. Every one was
                    checked on the live page today, and each shows what we saw so you can look for
                    yourself rather than take our word.
                  </div>

                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-top:24px;border-top:1px solid {BORDER};">
                    {rows}
                  </table>

                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin-top:28px;">
                    <tr>
                      <td style="background:{ACCENT};border-radius:9px;">
                        <a href="mailto:{escape(reply_to)}?subject=Fix%20these" style="display:inline-block;padding:13px 24px;font-family:{SANS};font-size:14px;font-weight:600;letter-spacing:-.1px;color:#ffffff;text-decoration:none;">Have these fixed</a>
                      </td>
                    </tr>
                  </table>

                  <div style="{NOTE}padding-top:16px;">
                    You approve each change before it happens and every one is re-checked afterwards,
                    so the before and after is on the record. Nothing is charged until you have a
                    result you agree with.
                  </div>

                </td>
              </tr>
            </table>
          </td>
        </tr>

        <tr>
          <td style="padding:20px 10px 0;font-family:{SANS};{FINE}">
            RevenueOS, Adelaide, South Australia &nbsp;&middot;&nbsp;
            <a href="mailto:{escape(reply_to)}" style="color:{MUTED};text-decoration:none;">{escape(reply_to)}</a><br>
            Not interested? Reply "stop" and that is the end of it.
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body></html>"""
