"""The findings email: the one message a stranger receives from RevenueOS.

It has to survive two readers at once. The first is a spam filter, which has never heard of this
domain. The second is an owner who gets four of these a week from people who want to rebuild their
website, and who decides in about six seconds whether this is another one.

**The first screen is the finding, not us.** A cold email that opens by introducing the sender
spends its whole attention budget on the least interesting party. So the page name, the defect and
the literal line from their own HTML come first. Seeing your own markup quoted back is the one
thing a mass mailer cannot fake, and it is checkable in ten seconds without replying to anyone.

**Who is writing, and why they looked, comes immediately after.** That ordering is deliberate.
Identity placed first is ignored; identity placed at the moment the reader has just thought "who
is this" is read. It is also what the Spam Act asks for in substance: accurate information about
the sender and how to contact them, present in the message itself.

**Nothing in it is scraped-looking.** A tracking pixel ID or a container ID quoted back at someone
is public in their page source and still reads like a stranger going through your drawers. It also
tells the reader nothing they can act on. `_check_evidence` refuses to render evidence containing
one, the same way the send path refuses copy containing a dash: a rule in code beats a rule someone
has to remember.

**Exactly one link, and it points at the domain the mail came from.** Zero links is defensible but
leaves a stranger no way to confirm we exist. A link to revenueos.com.au from a message sent by
hello@revenueos.com.au is the cheapest honesty signal available, and the alignment is the thing a
phishing message cannot reproduce.

**The mark is 48px, which is the smallest size its own artwork survives.** Rendered side by side at
32 through 64, the stems between its dots close up below 48 and it reads as a fuzzy disc. That is
the drawing, not the resampling, so the asset is rendered straight from the vector at twice the
display size and the display size is the one the drawing needs.

**The lockup is centred, with one mark height of clear space under it.** Left aligned against the
top corner it sat in the weakest position available, competing with the eyebrow directly beneath.
Airtable, Mixpanel, Otter and HoneyBook all centre a small lockup over the card instead, so the
brand reads once and then gets out of the way. Horizontal rather than stacked, which is the default
in every brand system that bothers to say; stacked would spend real height on a name the reader
already has from the From line.

**It is complete with images switched off**, which is the default state for a first contact from an
unknown sender. The wordmark is live text next to the mark, the evidence panel is a bordered block
rather than a picture, and no part of the argument lives inside an image.

Email constraints shape the rest. No <style> block and no media queries, because coverage is still
uneven and inline styles need neither. Tables with inline styles throughout, since Outlook renders
through Word. A hidden preheader controls the inbox preview line, and it says something the subject
does not rather than repeating it. Type sizes come from a scale with tracking tied to size: tight
on the hero because letters read too far apart as they grow, near zero on body, open on small caps.

There is no sender location anywhere in it. The owner asked for that out of outbound mail,
and Australia does not require a postal address in commercial email; that is the American
rule, copied here by reflex. The Spam Act asks for accurate identification of the sender
and a contact that stays valid, which the company name, the mailbox and the site carry.

Every claim rendered here must have been verified against the live page first. The crawler once
produced findings about phone numbers on pages that display none, and quoting a customer's own HTML
back at them is worthless the moment one line of it is wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

# Palette, taken from the product site's own stylesheet.
INK, SOFT, MUTED = "#0f172a", "#44506b", "#6b7280"
ACCENT = "#3d3ff0"
BORDER, HAIR, PAGE, PANEL, QUOTE = "#e3e5ea", "#eceef2", "#f4f4f2", "#ffffff", "#f7f8fa"

MARK = "https://revenueos.com.au/press/revenueos-mark-accent-96.png"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

HERO = f"font-size:24px;line-height:1.2;letter-spacing:-.55px;font-weight:700;color:{INK};"
EYEBROW = f"font-family:{MONO};font-size:11.5px;line-height:1.3;letter-spacing:.1px;color:{MUTED};"
LEAD = f"font-size:15px;line-height:1.62;letter-spacing:-.08px;color:{SOFT};"
ITEM = f"font-size:14.5px;line-height:1.4;letter-spacing:-.14px;font-weight:600;color:{INK};"
BODY = f"font-size:13.5px;line-height:1.55;letter-spacing:-.04px;color:{SOFT};"
TERM = f"font-family:{MONO};font-size:12px;line-height:1.65;color:{INK};word-break:break-word;"
CAP = f"font-family:{MONO};font-size:10.5px;line-height:1;letter-spacing:.9px;color:{MUTED};"
NOTE = f"font-size:13.5px;line-height:1.62;letter-spacing:-.02px;color:{SOFT};"
NUM = f"font-family:{MONO};font-size:11px;line-height:1;letter-spacing:.4px;color:{ACCENT};font-weight:600;"
FINE = f"font-size:11.5px;line-height:1.62;letter-spacing:.06px;color:{MUTED};"
SIG = f"font-size:12px;line-height:1.62;letter-spacing:-.01px;color:{SOFT};"

# Identifiers that are public in a page's source and still read as surveillance when quoted back.
# Meta pixel ids, Google Ads conversion ids, GA4 and Universal Analytics ids, GTM containers.
_SNOOPED = re.compile(r"\b(?:AW-\d{6,}|UA-\d{4,}-\d+|G-[A-Z0-9]{8,}|GTM-[A-Z0-9]{5,}|\d{15,17})\b")


@dataclass(frozen=True)
class Sender:
    """Who the message is from, rendered in the footer exactly as given.

    Nothing here is inferred or filled in with a plausible default. A blank `abn` renders no ABN
    rather than an invented one, because a wrong company number is worse than an absent one.
    """
    company: str = "RevenueOS"
    person: str = ""
    email: str = "hello@revenueos.com.au"
    site: str = "revenueos.com.au"
    abn: str = ""

    def identity_line(self) -> str:
        """The sender, named. No location: the owner asked for it out of outbound mail, and
        Australia does not require a postal address in commercial email the way the United
        States does."""
        return self.company + (f" · ABN {self.abn}" if self.abn else "")

    @property
    def i(self) -> str:
        """The subject pronoun. A named person writes as I; a brand writes as we. Signing a
        first person singular letter with a company name reads as a name being withheld."""
        return "I" if self.person else "we"

    @property
    def will(self) -> str:
        return "I will" if self.person else "we will"


@dataclass(frozen=True)
class Finding:
    """One verified problem on the customer's site.

    `evidence` is the literal observation, quoted so the reader can check it rather than trust us.
    `cost` says why it matters in their terms, not ours.
    """
    title: str
    evidence: str
    cost: str


def _check_evidence(f: Finding) -> None:
    hit = _SNOOPED.search(f.evidence)
    if hit:
        raise ValueError(
            f"refusing to render evidence quoting {hit.group(0)!r}. A tracking identifier read out "
            "of someone's page source reads as surveillance and tells them nothing they can act "
            f"on. Say what fires, not its number, in the finding {f.title!r}.")


def _lines(evidence: str) -> str:
    """Each line of the evidence on its own line. The quote and what we read from it are two
    different kinds of statement and should not run together into an orphaned word."""
    return "<br>".join(escape(ln) for ln in evidence.split("\n"))


def _quote(evidence: str) -> str:
    """The evidence, in a bordered block with an accent rule, so it reads as a quotation from
    their page rather than a claim of ours. Light rather than a dark terminal panel: the panel
    looked like a growth hacker's screenshot, and long markup lines overflowed it on a phone."""
    return f"""<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
                    <tr>
                      <td width="3" style="width:3px;background:{ACCENT};font-size:0;line-height:0;">&nbsp;</td>
                      <td style="background:{QUOTE};border:1px solid {HAIR};border-left:0;padding:13px 15px;">
                        <div style="{TERM}">{_lines(evidence)}</div>
                      </td>
                    </tr>
                  </table>"""


def _row(f: Finding, i: int, last: bool) -> str:
    rule = "" if last else f"border-bottom:1px solid {HAIR};"
    return f"""
                <tr>
                  <td style="padding:15px 0;{rule}">
                    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
                      <tr>
                        <td width="26" style="width:26px;vertical-align:top;padding-top:3px;">
                          <span style="{NUM}">{i:02d}</span>
                        </td>
                        <td style="vertical-align:top;">
                          <div style="{ITEM}">{escape(f.title)}</div>
                          <div style="{BODY}padding-top:5px;">{escape(f.cost)}</div>
                          <div style="font-family:{MONO};font-size:11.5px;line-height:1.5;color:{MUTED};padding-top:7px;word-break:break-word;">{_lines(f.evidence)}</div>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>"""


def _why_default(s: Sender) -> str:
    if s.person:
        return (f"I am {s.person} and I build {s.company}. I read this page myself before "
                "writing to you, and every line above is quoted from it rather than produced "
                "by a scanner.")
    return (f"We are {s.company}. A person read this page before this was sent, and every "
            "line above is quoted from it rather than produced by a scanner.")


def _indent(evidence: str) -> str:
    return "\n".join(f"    {ln}" for ln in evidence.split("\n"))


def _basis_default(s: Sender) -> str:
    """Why this arrived, said plainly.

    It is also the substance of the inferred consent the Australian Spam Act turns on: an address
    published in a business capacity, a message relevant to that capacity, and a way out. Stating
    the basis is what separates a first contact from a campaign, and it is the sentence that stops
    a reader reporting the message instead of replying to it.
    """
    them = "me" if s.person else "us"
    return ("Your address is published on your own site and this message is about that site, which "
            f"is the only reason we have it. Reply with the word stop and you will not hear from "
            f"{them} again.")


def findings_text(*, business: str, page: str, findings: list[Finding],
                  sender: Sender = Sender(), why: str = "", basis: str = "") -> str:
    """The plain text alternative. It carries the same claims, the same identity and the same way
    out, because a message whose text part is a stub saying "view this in a browser" is the shape
    of a message with something to hide, and filters score it that way."""
    if not findings:
        raise ValueError("an email with no findings has nothing to say")
    for f in findings:
        _check_evidence(f)
    hero, rest = findings[0], findings[1:]
    why = why or _why_default(sender)
    basis = basis or _basis_default(sender)

    # Underlined, not upper cased. A whole sentence in capitals reads as shouting to a person and
    # scores as shouting in a filter, and the rule is the plain text convention in any case.
    lines = [page, "", hero.title, "=" * len(hero.title), "", hero.cost, "",
             "From your page, today:", _indent(hero.evidence), "", why, ""]
    if rest:
        lines += [f"{len(rest)} more on the same page:" if len(rest) > 1 else "One more on the same page:", ""]
        for i, f in enumerate(rest, start=2):
            lines += [f"{i:02d}  {f.title}", f"    {f.cost}", _indent(f.evidence), ""]
    lines += [
        f"{business}, reply and {sender.will} send the fixes. You approve each one before "
        "anything changes, and every one is re-checked afterwards. Nothing is charged until a "
        "result is measured.", "",
        sender.identity_line(),
        f"{sender.email}  {sender.site}", "",
        basis,
    ]
    return "\n".join(lines)


def findings_email(*, business: str, page: str, findings: list[Finding],
                   sender: Sender = Sender(), why: str = "", basis: str = "") -> str:
    """Render the complete HTML email. The first finding becomes the hero."""
    if not findings:
        raise ValueError("an email with no findings has nothing to say")
    for f in findings:
        _check_evidence(f)

    hero, rest = findings[0], findings[1:]
    why = why or _why_default(sender)
    basis = basis or _basis_default(sender)

    more = ""
    if rest:
        rows = "".join(_row(f, i + 2, i == len(rest) - 1) for i, f in enumerate(rest))
        label = "ONE MORE" if len(rest) == 1 else f"{len(rest)} MORE"
        more = f"""
                  <div style="{CAP}padding-top:28px;">{label} ON THE SAME PAGE</div>
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-top:8px;border-top:1px solid {HAIR};">
                    {rows}
                  </table>"""

    site = escape(sender.site)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light only">
<title>{escape(hero.title)}</title></head>
<body style="margin:0;padding:0;background:{PAGE};-webkit-text-size-adjust:100%;">
<div style="display:none;font-size:1px;color:{PAGE};line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">Quoted from {escape(page)}, read by hand this morning. Three things, and what each one costs you.</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;background:{PAGE};">
  <tr>
    <td align="center" style="padding:36px 16px 44px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="580" style="border-collapse:collapse;width:580px;max-width:100%;">

        <tr>
          <td align="center" style="padding:0 0 48px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="border-collapse:collapse;"><tr>
              <td width="48" style="width:48px;padding-right:13px;vertical-align:middle;">
                <img src="{MARK}" width="48" height="48" alt="" style="display:block;width:48px;height:48px;border:0;">
              </td>
              <td style="vertical-align:middle;font-family:{SANS};font-size:17px;line-height:1;font-weight:600;letter-spacing:.05px;color:{INK};">{escape(sender.company)}</td>
            </tr></table>
          </td>
        </tr>

        <tr>
          <td style="background:{PANEL};border:1px solid {BORDER};border-radius:14px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
              <tr>
                <td style="padding:30px 26px 28px;font-family:{SANS};">

                  <div style="{EYEBROW}">{escape(page)}</div>
                  <div style="{HERO}padding-top:9px;">{escape(hero.title)}</div>
                  <div style="{LEAD}padding-top:11px;">{escape(hero.cost)}</div>

                  <div style="{CAP}padding-top:22px;padding-bottom:9px;">FROM YOUR PAGE, TODAY</div>
                  {_quote(hero.evidence)}

                  <div style="{NOTE}padding-top:18px;">{escape(why)}</div>
                  {more}

                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;margin-top:26px;border-top:1px solid {HAIR};">
                    <tr><td style="padding-top:19px;">
                      <div style="{NOTE}">
                        {escape(business)}, reply and {sender.will} send the fixes. You approve
                        each one before anything changes, and every one is re-checked afterwards.
                        Nothing is charged until a result is measured.
                      </div>
                    </td></tr>
                  </table>

                </td>
              </tr>
            </table>
          </td>
        </tr>

        <tr>
          <td style="padding:19px 12px 0;font-family:{SANS};">
            <div style="{SIG}"><span style="font-weight:600;color:{INK};">{escape(sender.identity_line())}</span></div>
            <div style="{SIG}">{escape(sender.email)} &nbsp;&middot;&nbsp; <a href="https://{site}" style="color:{SOFT};text-decoration:underline;">{site}</a></div>
            <div style="{FINE}padding-top:9px;">{escape(basis)}</div>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body></html>"""
