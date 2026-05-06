"""
Explore NCUC portal document search pages to find filing documents.
"""
from duke_rates.config import Settings
from duke_rates.historical.ncuc.session import create_authenticated_context, close_authenticated_context

settings = Settings()
pw, ctx, page = create_authenticated_context(settings)

try:
    # 1. Explore DocumentsParameterSearch
    print("=== DocumentsParameterSearch ===")
    url = "https://starw1.ncuc.gov/NCUC/page/DocumentsParameterSearch/portal.aspx"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    print(f"Title: {page.title()}")

    inputs = page.query_selector_all("input, select, textarea")
    print(f"Form fields: {len(inputs)}")
    for inp in inputs:
        name = inp.get_attribute("name") or inp.get_attribute("id") or ""
        val = inp.get_attribute("value") or ""
        placeholder = inp.get_attribute("placeholder") or ""
        inp_type = inp.get_attribute("type") or inp.evaluate("el => el.tagName.toLowerCase()")
        label = ""
        try:
            label_el = page.query_selector(f'label[for="{inp.get_attribute("id")}"]')
            if label_el:
                label = label_el.inner_text().strip()
        except:
            pass
        print(f"  [{inp_type}] name={name} label={label!r} val={val[:30]!r} placeholder={placeholder[:30]!r}")

    # Look at the page content for form structure
    content = page.content()
    # Find form action or submit URL
    import re
    actions = re.findall(r'action=["\']([^"\']+)["\']', content)
    print(f"\nForm actions: {actions[:5]}")

    # 2. Try filling in E-2, Sub 1354 and submitting
    print("\n=== Searching for E-2, Sub 1354 ===")
    # Look for docket/case number fields
    docket_inputs = page.query_selector_all("input[id*='ocket'], input[name*='ocket'], input[id*='ase'], input[name*='ase']")
    print(f"Docket-like inputs: {len(docket_inputs)}")
    for inp in docket_inputs:
        print(f"  id={inp.get_attribute('id')} name={inp.get_attribute('name')}")

    # Try typing in the first text input to see what labels are visible
    all_labels = page.query_selector_all("label")
    print(f"\nAll labels ({len(all_labels)}):")
    for lbl in all_labels:
        print(f"  [{lbl.inner_text().strip()[:60]}] for={lbl.get_attribute('for')}")

    # Get a meaningful snippet of the page body
    body_text = page.inner_text("body")
    print(f"\nPage body text (first 1500):\n{body_text[:1500]}")

finally:
    close_authenticated_context(pw, ctx)
