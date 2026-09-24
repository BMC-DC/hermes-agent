"""Deterministic HTML assembly for the monthly newsletter.

Reproduces Email/skills/references/design-system.md's Section 4 (fixed
shell), Section 5 (component library), and Section 9 (assembly skeleton) as
plain Python string templates — Stylus never writes or reproduces HTML
markup itself, only plain text (see pipeline.py's ``DRAFT_METADATA_FIELDS``
and Stylus's SOUL.md "Monthly newsletter drafting" section). This is the
same "Stylus writes words, code does layout" split the FB/IG/blog flow
already uses — see the drafting-instructions rewrite this module shipped
alongside for why the newsletter pipeline previously violated it (Stylus
was asked to produce ``assembled_html`` itself, which produced shallow,
barely-drafted output and duplicated the design system's markup knowledge
inside an LLM instead of in code, where it belongs).

If Section 10's skeleton ever changes shape, update this module and
Stylus's ``email-newsletter``/``examples-newsletter`` skills in the same
change — this file IS the design system's Section 4/5/9, not an
independent reimplementation of it; keep it in sync by hand the same way
the three copies of design-system.md itself must move together (see
Email/CLAUDE.md).
"""

from __future__ import annotations

from html import escape
from typing import Any, Optional

LOGO_URL = "https://buddhameditationdc.org/wp-content/uploads/2026/07/logo.png"
BLOG_INDEX_URL = "https://buddhameditationdc.org/blog/"
CALENDAR_URL = "https://buddhameditationdc.org/meditation-events-calendar/"
NAVY = "#1C244B"
MUTED = "#324A6D"


def _paragraphs(text: str, *, color: str = NAVY) -> str:
    """One <tr> per "\\n\\n"-separated paragraph — 18px bottom padding between
    paragraphs, 8px after the last one, per design-system.md Section 5.1."""
    parts = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    rows = []
    for i, part in enumerate(parts):
        bottom = 8 if i == len(parts) - 1 else 18
        rows.append(
            f'<tr><td class="bmc-font" style="font-family:\'Poppins\',\'Segoe UI\',Verdana,Arial,sans-serif; '
            f'font-size:16px; line-height:1.6; color:{color}; padding-bottom:{bottom}px;">{escape(part)}</td></tr>'
        )
    return (
        '<tr><td class="bmc-pad" style="padding:0 40px 0 40px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        + "".join(rows) + "</table></td></tr>"
    )


def _pull_quote(text: str) -> str:
    return f"""<tr>
  <td class="bmc-pad" style="padding:36px 40px 36px 40px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td width="4" style="background-color:#D68B4B; border-radius:4px; font-size:1px; line-height:1px;">&nbsp;</td>
        <td width="18" style="font-size:1px; line-height:1px;">&nbsp;</td>
        <td>
          <span class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:17px; font-weight:500; font-style:italic; line-height:1.6; color:{NAVY};">
            {escape(text)}
          </span>
        </td>
      </tr>
    </table>
  </td>
</tr>"""


def _photo(url: str, alt: str, *, square: bool = False) -> str:
    width = 480 if square else 520
    return (
        f'<tr><td class="bmc-pad" style="padding:0 40px 32px 40px;">'
        f'<img src="{escape(url)}" width="{width}" alt="{escape(alt)}" '
        f'style="display:block; width:100%; max-width:{width}px; height:auto; border:0; border-radius:14px; margin:0 auto;">'
        f"</td></tr>"
    )


def _primary_button(label: str, url: str) -> str:
    return f"""<tr>
  <td align="center" style="padding:20px 40px 8px 40px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td align="center" bgcolor="#D68B4B" style="border-radius:9999px;">
          <!--[if mso]>
          <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{escape(url)}" style="height:46px;v-text-anchor:middle;width:220px;" arcsize="50%" strokecolor="#D68B4B" fillcolor="#D68B4B">
          <w:anchorlock/>
          <center style="color:#ffffff; font-family:Segoe UI, Arial, sans-serif; font-size:15px; font-weight:600;">{escape(label)}</center>
          </v:roundrect>
          <![endif]-->
          <!--[if !mso]><!-->
          <a href="{escape(url)}" target="_blank" class="bmc-btn-primary">{escape(label)}</a>
          <!--<![endif]-->
        </td>
      </tr>
    </table>
  </td>
</tr>"""


def _secondary_button(label: str, url: str) -> str:
    return (
        f'<tr><td align="center" style="padding:16px 40px 32px 40px;">'
        f'<a href="{escape(url)}" target="_blank" class="bmc-btn-secondary">{escape(label)}</a>'
        f"</td></tr>"
    )


def _subtitle(text: str) -> str:
    return f"""<tr>
  <td class="bmc-pad" style="padding:36px 40px 8px 40px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:22px; font-weight:600; line-height:1.4; color:{NAVY}; padding-bottom:8px;">
          {escape(text)}
        </td>
      </tr>
    </table>
  </td>
</tr>"""


def _sign_off(signoff_line: str, signature: str) -> str:
    return f"""<tr>
  <td class="bmc-pad" style="padding:28px 40px 48px 40px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr><td style="padding-bottom:16px;"><div style="border-top:1px solid #E7E2DC; line-height:1px; font-size:1px;">&nbsp;</div></td></tr>
      <tr>
        <td class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:15px; line-height:1.6; color:{MUTED};">
          {escape(signoff_line)}<br>
          <strong style="color:{NAVY};">{escape(signature)}</strong>
        </td>
      </tr>
    </table>
  </td>
</tr>"""


def _hero(eyebrow_label: str, hero_headline: str) -> str:
    return f"""<tr>
  <td style="background-color:#FFFFFF; padding:16px 16px 0 16px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{NAVY}; border-radius:20px;">
      <tr>
        <td align="center" class="bmc-hero-pad" style="padding:48px 40px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td align="center" style="padding-bottom:14px;">
                <span class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:11px; font-weight:600; letter-spacing:2.4px; text-transform:uppercase; color:#D68B4B;">
                  {escape(eyebrow_label)}
                </span>
              </td>
            </tr>
            <tr>
              <td align="center" class="bmc-h1">
                <span class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:26px; font-weight:600; line-height:1.3; color:#FFFFFF;">
                  {escape(hero_headline)}
                </span>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </td>
</tr>"""


def _header() -> str:
    return f"""<tr>
  <td align="center" style="background-color:#FFFFFF; padding:32px 24px 8px 24px;">
    <a href="https://buddhameditationdc.org" target="_blank" style="text-decoration:none;">
      <img src="{LOGO_URL}" width="170" alt="Buddha Meditation Center of Greater Washington, DC" style="display:block; width:170px; max-width:170px; height:auto; border:0; margin:0 auto;">
    </a>
  </td>
</tr>
<tr>
  <td align="center" style="background-color:#FFFFFF; padding:0 24px 28px 24px;">
    <span class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:11px; font-weight:500; letter-spacing:1.8px; text-transform:uppercase; color:{MUTED};">
      Buddha Meditation Center &nbsp;&middot;&nbsp; Greater Washington, DC
    </span>
  </td>
</tr>"""


def _footer() -> str:
    return f"""<tr>
  <td style="background-color:#FFFFFF; padding:0 16px 16px 16px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{NAVY}; border-radius:20px;">
      <tr><td style="padding:40px 40px 32px 40px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td align="center" class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:13px; font-weight:600; letter-spacing:0.4px; padding-bottom:24px;">
              <a href="https://web.facebook.com/buddhameditationdc" target="_blank" style="color:#FFFFFF; text-decoration:underline;">Facebook</a>
              &nbsp;&middot;&nbsp;
              <a href="https://www.instagram.com/buddha_meditation_center_dc/" target="_blank" style="color:#FFFFFF; text-decoration:underline;">Instagram</a>
              &nbsp;&middot;&nbsp;
              <a href="https://www.youtube.com/channel/UC0q52NkAuxW2fgGnDsxY_BA" target="_blank" style="color:#FFFFFF; text-decoration:underline;">YouTube</a>
            </td>
          </tr>
          <tr>
            <td align="center" class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:13px; line-height:1.7; color:rgba(255,255,255,0.75); padding-bottom:16px;">
              Buddha Meditation Center &nbsp;&middot;&nbsp; 5004 Stone Rd, Rockville, MD 20853<br>
              +1 762 233 3390 &nbsp;&middot;&nbsp; info@buddhameditationdc.org
            </td>
          </tr>
          <tr><td style="padding-bottom:16px;"><div style="border-top:1px solid rgba(255,255,255,0.15); line-height:1px; font-size:1px;">&nbsp;</div></td></tr>
          <tr>
            <td align="center" class="bmc-font" style="font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif; font-size:12px; line-height:1.7; color:rgba(255,255,255,0.55);">
              You're receiving this because you connected with Buddha Meditation Center.<br>
              <a href="{{{{ unsubscribe }}}}" style="color:rgba(255,255,255,0.75); text-decoration:underline;">Unsubscribe</a>
            </td>
          </tr>
        </table>
      </td></tr>
    </table>
  </td>
</tr>"""


_HTML_SKELETON = """<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<![endif]-->
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap" rel="stylesheet">
<!--[if !mso]><!-->
<style>
  body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
  table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
  img {{ -ms-interpolation-mode: bicubic; border: 0; height: auto; line-height: 100%; outline: none; text-decoration: none; }}
  body {{ margin: 0; padding: 0; width: 100% !important; height: 100% !important; background-color: #FDFCFB; }}
  .bmc-font {{ font-family: 'Poppins', 'Segoe UI', Verdana, Arial, sans-serif; }}
  .bmc-btn-primary {{
    display:inline-block; font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif;
    font-size:15px; font-weight:600; padding:13px 34px; border-radius:9999px;
    text-decoration:none; background-color:#D68B4B; color:#FFFFFF !important;
    border:2px solid #D68B4B;
  }}
  .bmc-btn-primary:hover {{
    background-color:#FFFFFF !important; color:#D68B4B !important; border-color:#D68B4B !important;
  }}
  .bmc-btn-secondary {{
    display:inline-block; font-family:'Poppins','Segoe UI',Verdana,Arial,sans-serif;
    font-size:14px; font-weight:600; padding:11px 30px; border-radius:9999px;
    text-decoration:none; background-color:#FFFFFF; color:#D68B4B !important;
    border:2px solid #D68B4B;
  }}
  .bmc-btn-secondary:hover {{
    background-color:#D68B4B !important; color:#FFFFFF !important; border-color:#D68B4B !important;
  }}
  @media screen and (max-width: 600px) {{
    .bmc-wrapper {{ width: 100% !important; }}
    .bmc-pad {{ padding-left: 24px !important; padding-right: 24px !important; }}
    .bmc-hero-pad {{ padding: 40px 24px !important; }}
    .bmc-h1 {{ font-size: 22px !important; }}
    .bmc-social-td {{ padding: 0 6px !important; }}
  }}
</style>
<!--<![endif]-->
</head>
<body style="margin:0; padding:0; background-color:#FDFCFB;">

  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all; font-size:1px; line-height:1px; color:#FDFCFB; opacity:0;">
    {preheader}
  </div>
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;</div>

  <center style="width:100%; background-color:#FDFCFB;">
  <div style="max-width:600px; margin:0 auto;" class="bmc-wrapper">
  <!--[if mso]>
  <table role="presentation" width="600" align="center" cellpadding="0" cellspacing="0" border="0"><tr><td>
  <![endif]-->

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px; margin:0 auto; background-color:#FFFFFF;" class="bmc-wrapper">

{body}

  </table>

  <!--[if mso]>
  </td></tr></table>
  <![endif]-->
  </div>
  </center>

</body>
</html>"""


# --- Per-section HTML (also stored individually in newsletter_drafts, for
# potential future granular display — the review page currently only shows
# the full assembled_html, but these are exposed publicly so pipeline.py can
# populate the schema's *_html fields from the same building blocks rather
# than duplicating markup knowledge a second time). -------------------------


def bhante_advice_section_html(paragraph: str, quote: Optional[str]) -> str:
    html = _subtitle("Bhante's Advice") + "\n" + _paragraphs(paragraph)
    if quote:
        html += "\n" + _pull_quote(quote)
    return html


def recap_section_html(paragraph: str, image: Optional[dict[str, Any]]) -> str:
    html = _subtitle("Recently at BMC")
    if image and image.get("url"):
        alt = paragraph.strip().split(".")[0][:150] or "Photo from this month at Buddha Meditation Center"
        html += "\n" + _photo(image["url"], alt)
    html += "\n" + _paragraphs(paragraph, color=MUTED) + "\n" + _secondary_button("Read More", BLOG_INDEX_URL)
    return html


def featured_announcement_section_html(paragraph: str, cta_label: str, cta_url: str) -> str:
    return (
        _subtitle("Featured Announcement") + "\n" + _paragraphs(paragraph) + "\n"
        + _primary_button(cta_label, cta_url)
    )


def programs_section_html(paragraph: str) -> str:
    return (
        _subtitle("This Month's Programs") + "\n" + _paragraphs(paragraph) + "\n"
        + _secondary_button("Events Calendar", CALENDAR_URL)
    )


def render_newsletter_html(
    *,
    subject_line: str,
    preview_text: str,
    eyebrow_label: str,
    hero_headline: str,
    bhante_advice_html: str,
    recap_html: str,
    featured_announcement_html: str,
    programs_html: str,
) -> str:
    """Assembles the full newsletter page from the per-section HTML above —
    the deterministic counterpart to Stylus's plain-text drafting contract
    (see pipeline.py/SOUL.md). No HTML ever comes from Stylus; every tag
    here is a fixed template from design-system.md Sections 4/5/9."""
    body = "\n\n".join([
        _hero(eyebrow_label, hero_headline), _header(),
        bhante_advice_html, recap_html, featured_announcement_html, programs_html,
        _sign_off("With loving-kindness,", "Buddha Meditation Center"), _footer(),
    ])
    return _HTML_SKELETON.format(title=escape(subject_line), preheader=escape(preview_text), body=body)
