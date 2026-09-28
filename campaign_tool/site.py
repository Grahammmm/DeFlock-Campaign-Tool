"""Neutral offline starter using the reusable campaign shell and styles."""
from html import escape
from .shell import render


def preview(config):
    # Explicit public allowlist; unrelated settings and private data are ignored.
    name, county, state = (escape(config[key], quote=True)
                           for key in ("name", "county", "state"))
    return render({
        "schema_version": 1,
        "language": "en",
        "html": {
            "head": f'  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <meta name="description" content="A local initiative for transparent ALPR policies and evidence-based public participation.">\n  <title>{name} | Local accountability</title>\n  <link rel="stylesheet" href="style.css">\n',
            "header": f'  <a class="skip-link" href="#records">Skip to our process</a>\n  <header class="topbar campaign-banner"><div class="banner-intro"><a class="brand" href="#top">{name}</a><h1>Local records. Public understanding.</h1><p>{county}, {state}</p></div><a class="banner-signup-link" href="#participate">Get involved</a></header>\n',
            "main": f'    <section class="section" id="records"><div class="section-heading"><div><p class="eyebrow">Evidence before conclusions</p><h2>Know how your community uses license plate readers.</h2></div><p>We are building a source-backed picture of policies, contracts and oversight in {county}. Questions deserve records, not assumptions.</p></div><div class="mission-grid"><article><span>01</span><h3>Request</h3><p>Ask for policies, agreements, sharing settings and audit records.</p></article><article><span>02</span><h3>Understand</h3><p>Preserve originals, compare applicable rules and independently challenge every material finding.</p></article><article><span>03</span><h3>Participate</h3><p>Use reviewed evidence and official meeting channels to ask informed questions.</p></article></div></section>\n    <section class="section movement-section" id="participate"><h2>A campaign built on evidence.</h2><p>This is a starter preview. Contacts, meetings, maps, newsletter signup and findings have not been configured.</p><p>No email addresses are collected by this preview.</p></section>\n',
            "footer": '  <footer class="site-footer"><p>Independent local initiative. No agency findings are asserted by this starter. Built with DeFlock Campaign Tool.</p></footer>\n'
        },
        "styles": {"font_family": "Georgia,serif", "font_faces": ""}
    })
