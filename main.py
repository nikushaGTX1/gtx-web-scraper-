import requests
from bs4 import BeautifulSoup
import json
from pathlib import Path
import re
import sys
import os
from datetime import datetime
from urllib.parse import urljoin, urlparse


# A frozen Windows executable can otherwise inherit a legacy console code page
# (for example cp1252), which cannot display Georgian prompts and labels.
# Setting both console code pages before wrapping stdout/stderr keeps output
# readable when the program is launched by double-clicking the .exe.
def configure_windows_console():
    """Use UTF-8 and a font that contains Georgian glyphs on Windows."""

    if os.name != "nt":
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleOutputCP(65001)  # UTF-8
    kernel32.SetConsoleCP(65001)        # UTF-8 input

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class CONSOLE_FONT_INFOEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.ULONG),
            ("nFont", wintypes.DWORD),
            ("dwFontSize", COORD),
            ("FontFamily", wintypes.UINT),
            ("FontWeight", wintypes.UINT),
            ("FaceName", wintypes.WCHAR * 32),
        ]

    # Segoe UI ships with Windows and includes Georgian characters.  Font
    # selection can fail in unusual console hosts, but UTF-8 output remains.
    console = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    font = CONSOLE_FONT_INFOEX()
    font.cbSize = ctypes.sizeof(font)
    if console and console != wintypes.HANDLE(-1).value:
        if kernel32.GetCurrentConsoleFontEx(console, False, ctypes.byref(font)):
            font.FaceName = "Segoe UI"
            kernel32.SetCurrentConsoleFontEx(console, False, ctypes.byref(font))


configure_windows_console()

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# FETCH HTML
# ============================================================

def fetch_html(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36"
        )
    }

    r = requests.get(url, headers=headers, timeout=20)

    if r.status_code != 200:
        raise Exception(f"ვერ ჩაიტვირთა: {r.status_code}")

    return r.text


# ============================================================
# HELPERS
# ============================================================

def first_value(data, *keys, default=None):
    """
    Finds the first existing value from several possible keys.
    """

    if not isinstance(data, dict):
        return default

    for key in keys:
        value = data.get(key)

        if value is not None and value != "":
            return value

    return default


def clean(value):
    if value is None:
        return ""

    return str(value).strip()


def number(value):
    """
    Converts values such as:
        '85.00'
        '85,00'
        '85 მ²'
    into numbers where possible.
    """

    if value is None:
        return None

    if isinstance(value, (int, float)):
        return value

    value = str(value).strip()

    if not value:
        return None

    value = value.replace(",", ".")
    value = re.sub(r"[^\d.]", "", value)

    if not value or value == ".":
        return None

    try:
        return float(value)
    except:
        return None


def rooms_and_bedrooms(rooms, bedrooms):
    """Formats the single workbook column for rooms and bedrooms."""

    parts = []
    if clean(rooms):
        parts.append(f"{clean(rooms)} ოთახი")
    if clean(bedrooms):
        parts.append(f"{clean(bedrooms)} საძ")
    return " ".join(parts) if parts else None


def square_meters(area):
    """Formats the workbook's area field, for example ``52კვ``."""

    if isinstance(area, float) and area.is_integer():
        area = int(area)
    value = clean(area)
    return f"{value}კვ" if value else None


# MyHome sometimes supplies only the broad administrative district (for
# example, „ვაკე-საბურთალო“) even though the listing URL names the actual
# neighborhood.  Prefer a real API field when available, then use this
# conservative URL fallback rather than storing a misleading broad region.
MYHOME_NEIGHBORHOOD_URL_LABELS = (
    ("saburtalo", "საბურთალო"),
    ("vake", "ვაკე"),
    ("dzvel-tbilis", "ძველი თბილისი"),
    ("chughuret", "ჩუღურეტი-კუკია"),
    ("didube", "დიდუბე"),
    ("ivertuban", "ივერთუბანი"),
    ("gldan", "გლდანი"),
    ("nadzaladev", "ნაძალადევი"),
    ("isani", "ისანი"),
    ("samgori", "სამგორი"),
    ("varketil", "ვარკეთილი"),
    ("dighomi", "დიღომი"),
    ("ortachala", "ორთაჭალა"),
)


def myhome_neighborhood(statement, url):
    """Returns MyHome's most specific available neighborhood."""

    specific = first_value(
        statement,
        "neighborhood_name",
        "neighbourhood_name",
        "subdistrict_name",
        "sub_district_name",
        "microdistrict_name",
    )
    if specific:
        return specific

    url_text = clean(url).casefold()
    for slug, label in MYHOME_NEIGHBORHOOD_URL_LABELS:
        if slug in url_text:
            return label

    return first_value(statement, "district_name", "district", "region_name")


def ss_neighborhood(address, url):
    """Returns SS.ge's most specific neighborhood, not its broad district."""

    specific = first_value(
        address,
        "neighborhoodTitle",
        "neighbourhoodTitle",
        "subdistrictTitle",
        "microdistrictTitle",
        "settlementTitle",
    )
    if specific:
        return specific

    # SS.ge slugs include the displayed neighborhood on many listings.
    url_text = clean(url).casefold()
    for slug, label in MYHOME_NEIGHBORHOOD_URL_LABELS:
        if slug in url_text:
            return label

    return first_value(address, "districtTitle", "municipalityTitle")


def label_myhome_code(value, labels):
    """Returns MyHome's Georgian label instead of an internal numeric ID."""

    if value is None:
        return None

    try:
        code = int(str(value).strip())
    except (TypeError, ValueError):
        return value

    return labels.get(code, value)


MYHOME_DEAL_LABELS = {
    1: "იყიდება",
    2: "ქირავდება",
    3: "ქირავდება დღიურად",
    4: "გირავდება",
}

MYHOME_PROPERTY_LABELS = {
    1: "ბინა",
    2: "სახლი",
    3: "აგარაკი",
    4: "კომერციული ფართი",
    5: "მიწის ნაკვეთი",
    6: "ოფისი",
    7: "საწყობი",
    8: "გარაჟი",
}

MYHOME_STATUS_LABELS = {
    1: "ძველი აშენებული",
    2: "ახალი აშენებული",
    3: "მშენებარე",
}


def extract_phones(raw):
    """
    Normalizes phone data that can arrive in several shapes:
        None
        "555123456"
        ["555123456", "599000000"]
        [{"phoneNumber": "555123456"}, ...]
        [{"number": "555123456"}, ...]
    Returns a single ", "-joined string, or None if nothing usable found.
    """

    if raw is None:
        return None

    if isinstance(raw, (str, int)):
        text = clean(raw)
        return text or None

    if isinstance(raw, dict):
        raw = [raw]

    if isinstance(raw, list):
        numbers = []

        for entry in raw:
            if isinstance(entry, dict):
                num = first_value(
                    entry,
                    "phoneNumber",
                    "phone_number",
                    "number",
                    "phone",
                    "value"
                )
            else:
                num = entry

            num = clean(num)

            if num:
                numbers.append(num)

        return ", ".join(numbers) if numbers else None

    return None


def extract_myhome_phone_from_page(soup):
    """Gets the number shown in MyHome's click-to-call button, if present."""

    # MyHome renders the revealed number as button text, e.g.
    # ``+995 599 510 140``. Looking only in buttons avoids accidentally
    # selecting a phone number from the listing description or site footer.
    phone_pattern = re.compile(
        r"(?:\+995\s*)?5\d{2}[\s-]*\d{3}[\s-]*\d{3}"
    )

    for button in soup.find_all("button"):
        match = phone_pattern.search(button.get_text(" ", strip=True))
        if match:
            return match.group(0)

    return None


def reveal_myhome_phone(url):
    """Clicks MyHome's phone button and returns the revealed number.

    MyHome deliberately sends a masked number in its regular HTML. Selenium
    uses an ordinary browser session to perform the same click as a visitor.
    """

    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        return None

    phone_pattern = re.compile(
        r"(?:\+995\s*)?5\d{2}[\s-]*\d{3}[\s-]*\d{3}"
    )
    # Importing Options directly keeps this dependency visible to PyInstaller.
    # ``webdriver.ChromeOptions`` is loaded lazily by Selenium and was absent
    # from an earlier EXE build.
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.page_load_strategy = "eager"
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    )
    driver = None

    try:
        print("\nMyHome ტელეფონის ნომერი მოწმდება...")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(15)
        driver.get(url)
        wait = WebDriverWait(driver, 12)

        # Click the button whose text has the site's masked mobile pattern,
        # for example: 599 510 ***.
        def masked_phone_button_present(d):
            for button in d.find_elements(By.TAG_NAME, "button"):
                if re.search(r"5\d{2}[\s-]*\d{3}[\s-]*\*{3}", button.text):
                    return button
            return False

        button = wait.until(masked_phone_button_present)
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();",
            button
        )

        def full_phone_present(d):
            match = phone_pattern.search(d.find_element(By.TAG_NAME, "body").text)
            return match.group(0) if match else False

        return wait.until(full_phone_present)
    except Exception as error:
        print(f"MyHome ტელეფონის ნომერი ვერ გაიხსნა: {error}")
        return None
    finally:
        if driver:
            driver.quit()


def extract_text(raw):
    """
    ss.ge's description/comment field can be either a plain string
    or a multi-language object like:
        {"ka": "...", "en": None, "ru": None, "text": "...", ...}
    Prefer Georgian, then any other populated language.
    """

    if raw is None:
        return None

    if isinstance(raw, str):
        return raw.strip() or None

    if isinstance(raw, dict):
        for key in ("ka", "text", "allLanguageTogather", "en", "ru"):
            value = raw.get(key)

            if isinstance(value, str) and value.strip():
                return value.strip()

        return None

    return clean(raw) or None


def normalize_field_name(value):
    """Makes JSON keys and Georgian/English labels comparable."""

    return re.sub(r"[^a-z0-9ა-ჰ]+", "", clean(value).casefold())


def simplify_attribute_value(value):
    """
    Returns a useful scalar from the value part of a MyHome attribute.

    MyHome uses both flat fields (for example ``has_elevator``) and
    attribute objects (for example ``{"name": "ლიფტი", "value": true}``).
    """

    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        for key in (
            "value", "selected", "is_selected", "enabled", "is_enabled",
            "available", "has_value", "name", "title", "label", "text",
            "ka", "name_ka", "title_ka"
        ):
            result = simplify_attribute_value(value.get(key))
            if result is not None and result != "":
                return result

    return None


def find_myhome_attribute(data, keys=(), labels=()):
    """
    Finds a MyHome listing value regardless of whether it is stored as a
    direct JSON key or in a nested labelled attribute list.

    Only the listing's ``statement`` object is searched, so site-wide
    configuration in __NEXT_DATA__ cannot be mistaken for listing data.
    """

    normalized_keys = {normalize_field_name(key) for key in keys}
    normalized_labels = [normalize_field_name(label) for label in labels]

    label_keys = {
        "name", "title", "label", "text", "key", "code", "type",
        "nameka", "titleka", "fieldname", "attributename", "parametername",
        "propertyname"
    }
    value_keys = (
        "value", "selected", "is_selected", "enabled", "is_enabled",
        "available", "has_value", "field_value", "attribute_value",
        "parameter_value", "property_value"
    )

    def label_matches(value):
        candidate = normalize_field_name(value)
        return bool(candidate) and any(
            label == candidate or label in candidate or candidate in label
            for label in normalized_labels
        )

    def search(value):
        if isinstance(value, dict):
            # Prefer an exact key match. This covers MyHome's flat fields.
            for key, child in value.items():
                if normalize_field_name(key) in normalized_keys:
                    result = simplify_attribute_value(child)
                    if result is not None and result != "":
                        return result

            # Attribute-list entries contain a label and a selected value.
            labels_in_entry = [
                child for key, child in value.items()
                if normalize_field_name(key) in label_keys and label_matches(child)
            ]
            if labels_in_entry:
                for key in value_keys:
                    result = simplify_attribute_value(value.get(key))
                    if result is not None and result != "":
                        return result

                # If a matching item is present in a list without a separate
                # value, the presence of that item means the feature is on.
                return True

            for child in value.values():
                result = search(child)
                if result is not None and result != "":
                    return result

        elif isinstance(value, list):
            for child in value:
                result = search(child)
                if result is not None and result != "":
                    return result

        return None

    return search(data)


def find_myhome_poster_type(soup, statement):
    """Returns the publisher badge displayed on a MyHome listing.

    The badge is rendered separately from the listing attributes, so fields
    such as ``owner_type`` are not reliable for deciding whether the poster
    is an owner, an agent, or an agency.
    """

    badge_labels = ("მესაკუთრე", "აგენტი", "სააგენტო")

    # Prefer the same text the visitor sees on the page.
    for tag in soup.find_all(("div", "span", "p")):
        label = clean(tag.get_text(" ", strip=True))
        if label in badge_labels:
            return label

    # Keep a JSON fallback for pages whose badge is not present in the HTML.
    value = find_myhome_attribute(
        statement,
        keys=("owner_type", "user_type", "poster_type", "seller_type"),
        labels=badge_labels + ("owner", "agent", "agency")
    )

    if isinstance(value, str):
        normalized = normalize_field_name(value)
        for label in badge_labels:
            if normalize_field_name(label) == normalized:
                return label

        english_labels = {
            "owner": "მესაკუთრე",
            "agent": "აგენტი",
            "agency": "სააგენტო",
        }
        if normalized in english_labels:
            return english_labels[normalized]

    return None


def resolve_myhome_currency(statement):
    """
    myhome.ge's "price" field is NOT a single price - it's a dict keyed
    by currency id, each holding the listing price converted into that
    currency:
        "price": {"1": {"price_total": 50, ...}, "2": {...}, "3": {...}}

    There is no explicit currency name anywhere in the statement. We
    find which bucket's price_total matches the top-level "total_price"
    (the currency the lister actually priced it in) and map that id to
    a currency code.

    ASSUMPTION (inferred from a single sample listing priced in GEL,
    not from documentation): key "1" = GEL, "2" = USD, "3" = EUR. This
    matched the site's own currency symbol on that listing, but should
    be re-verified against a listing priced in USD or EUR before relying
    on it for anything financial.
    """

    CURRENCY_ID_MAP = {"1": "GEL", "2": "USD", "3": "EUR"}

    price_obj = statement.get("price")
    total = statement.get("total_price")

    if not isinstance(price_obj, dict) or total is None:
        return None

    for key, bucket in price_obj.items():
        if isinstance(bucket, dict) and bucket.get("price_total") == total:
            return CURRENCY_ID_MAP.get(str(key))

    return None


# ============================================================
# SS.GE
# ============================================================

def parse_ss_ge(html, url=None):

    soup = BeautifulSoup(html, "html.parser")

    script = soup.find("script", id="__NEXT_DATA__")

    if not script:
        raise Exception("SS.ge მონაცემი ვერ მოიძებნა")

    data = json.loads(script.string)

    item = (
        data
        .get("props", {})
        .get("pageProps", {})
        .get("applicationData")
    )

    if not item:
        raise Exception("SS.ge-ზე განცხადება ვერ მოიძებნა")

    addr = item.get("address") or {}
    price = item.get("price") or {}

    # -------------------------
    # IMAGES
    # -------------------------

    images = []

    for img in item.get("appImages", []):
        if not isinstance(img, dict):
            continue

        image = (
            img.get("fileName")
            or img.get("large")
            or img.get("fullImage")
            or img.get("url")
        )

        if image:
            images.append(image)

    # -------------------------
    # BASIC DATA
    # -------------------------

    listing_id = item.get("applicationId")

    area = first_value(
        item,
        "totalArea",
        "areaOfHouse",
        "kitchenArea"
    )

    rooms = first_value(
        item,
        "rooms",
        "houseRooms"
    )

    bedrooms = item.get("bedrooms")

    floor = first_value(
        item,
        "floor"
    )

    total_floors = first_value(
        item,
        "floors"
    )

    # -------------------------
    # PRICE
    # -------------------------
    # priceGeo/priceUsd are mutually exclusive depending on how the
    # listing owner priced it - whichever is non-null tells us the
    # currency too, so we don't rely on currencyType alone.

    if price.get("priceGeo") is not None:
        price_value = price.get("priceGeo")
        currency = "GEL"
    elif price.get("priceUsd") is not None:
        price_value = price.get("priceUsd")
        currency = "USD"
    else:
        price_value = None
        currency = None

    # -------------------------
    # ADDRESS
    # -------------------------

    city = first_value(
        addr,
        "cityTitle"
    )

    street = first_value(
        addr,
        "streetTitle"
    )

    street_number = first_value(
        addr,
        "streetNumber"
    )

    district = ss_neighborhood(addr, url)

    address = " ".join(
        x for x in [
            clean(city),
            clean(street),
            clean(street_number)
        ]
        if x
    )

    # -------------------------
    # RETURN STANDARD FORMAT
    # -------------------------

    return {
        "ბინის ID": listing_id,

        "მესაკუთრის ID": first_value(
            item, "ownerId", "owner_id", "userId", "user_id", "contactPersonId"
        ),

        "მესაკუთრის ნომერი": extract_phones(item.get("applicationPhones")),

        "ოთახები და საძინებელი": rooms_and_bedrooms(rooms, bedrooms),

        "კვადრატულობა": square_meters(area),

        "აგენტის სახელი": first_value(
            item,
            "contactPerson",
            "companyName",
            "agencyName"
        ),

        "ტელეფონის ნომერი": extract_phones(
            item.get("applicationPhones")
        ),

        "განცხადების სახეობა": item.get("realEstateDealType"),

        "ქონების ტიპი": item.get("realEstateType"),

        "ქალაქი": city,

        "მისამართი": address,

        "უბანი": district,

        "ოთახები": rooms,

        "საძინებელი": bedrooms,

        "ფართობი (მ²)": area,

        "სართული": floor,

        "სართულიანობა": total_floors,

        "ფასი": price_value,

        "ვალუტა": currency,

        "ფასი / მ²": (
            round(float(price_value) / float(area), 2)
            if number(price_value) and number(area) and number(area) > 0
            else None
        ),

        "მდგომარეობა": first_value(
            item,
            "state",
            "floorTypeText"
        ),

        "სტატუსი": first_value(
            item,
            "status",
            "realEstateStatus"
        ),

        "გაყიდვა/ქირა": item.get("realEstateDealType"),

        "აგენტი": first_value(
            item,
            "agencyName",
            "companyName"
        ),

        # NOTE: ss.ge's applicationData has no dedicated "repair/renovation"
        # field in the sample we inspected - leaving this unmapped rather
        # than guessing a wrong key. Verify against a live listing if this
        # matters to you.
        "რემონტი": None,

        "ლიფტი": item.get("elevator"),

        # NOTE: no "hasParking" field exists; "garage" is the closest
        # equivalent but is not exactly the same concept - verify on a
        # live listing before relying on this.
        "პარკინგი": item.get("garage"),

        "ეზო": first_value(
            item,
            "areaOfYard",
            "viewOnYard"
        ),

        "ფოტო": len(images),

        "მიღების თარიღი": datetime.now().strftime("%Y-%m-%d"),

        "კომენტარი": extract_text(
            first_value(item, "description", "comment")
        ),

        "images": images
    }


# ============================================================
# MYHOME
# ============================================================

def parse_myhome(html, url=None):

    soup = BeautifulSoup(html, "html.parser")

    script = soup.find(
        "script",
        id="__NEXT_DATA__"
    )

    if not script:
        raise Exception("MyHome __NEXT_DATA__ not found")

    data = json.loads(script.string)

    statement = None

    queries = (
        data
        .get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )

    for q in queries:

        try:
            statement = (
                q["state"]
                ["data"]
                ["data"]
                ["statement"]
            )

            if statement:
                break

        except (KeyError, TypeError):
            continue

    if not statement:
        raise Exception("MyHome data not found")

    # ========================================================
    # IMAGES
    # ========================================================

    images = []

    for img in statement.get("images", []):

        if not isinstance(img, dict):
            continue

        image = (
            img.get("large")
            or img.get("full_image")
            or img.get("thumb")
            or img.get("url")
        )

        if image:
            images.append(image)

    # ========================================================
    # BASIC
    # ========================================================

    listing_id = first_value(
        statement,
        "statement_id",
        "id"
    )

    area = first_value(
        statement,
        "area",
        "total_area"
    )

    rooms = first_value(
        statement,
        "room_count",
        "rooms",
        "room_type_id"
    )

    bedrooms = first_value(
        statement,
        "bedroom_type_id",
        "bedrooms",
        "bedroom_count"
    )

    floor = first_value(
        statement,
        "floor"
    )

    total_floors = first_value(
        statement,
        "total_floors"
    )

    price = first_value(
        statement,
        "total_price",
        "price"
    )

    currency = resolve_myhome_currency(statement)

    # ========================================================
    # PRICE / M²
    # ========================================================

    price_m2 = None

    if number(price) and number(area) and number(area) > 0:
        price_m2 = round(
            number(price) / number(area),
            2
        )

    # ========================================================
    # ADDRESS
    # ========================================================

    address = first_value(
        statement,
        "address",
        "full_address"
    )

    city = first_value(
        statement,
        "city_name",
        "city"
    )

    district = myhome_neighborhood(statement, url)

    # These properties are not consistently flat fields in MyHome data.
    # Newer listings often keep them in nested, labelled attribute lists.
    poster_type = find_myhome_poster_type(soup, statement)
    contact_name = first_value(
        statement,
        "owner_name",
        "agent_name",
        "contact_name",
        "user_name"
    )
    agent_name = (
        contact_name if poster_type in ("აგენტი", "სააგენტო") else None
    )
    owner_name = contact_name if poster_type == "მესაკუთრე" else None

    deal_type = label_myhome_code(
        first_value(
            statement,
            "deal_type_name",
            "transaction_type_name",
            "operation_type_name",
            "deal_type",
            "transaction_type",
            "operation_type",
            "deal_type_id"
        ),
        MYHOME_DEAL_LABELS
    )
    property_type = label_myhome_code(
        first_value(
            statement,
            "property_type_name",
            "real_estate_type_name",
            "property_type",
            "real_estate_type_id"
        ),
        MYHOME_PROPERTY_LABELS
    )
    building_status = label_myhome_code(
        first_value(
            statement,
            "status_name",
            "building_status_name",
            "status",
            "status_id"
        ),
        MYHOME_STATUS_LABELS
    )

    elevator = find_myhome_attribute(
        statement,
        keys=(
            "elevator", "elevators", "has_elevator", "is_elevator",
            "lift", "lifts", "has_lift", "is_lift", "elevator_type",
            "elevator_type_id"
        ),
        labels=("ლიფტი", "სატვირთო ლიფტი", "elevator", "lift")
    )

    yard = find_myhome_attribute(
        statement,
        keys=(
            "yard", "has_yard", "is_yard", "yard_area", "area_of_yard",
            "courtyard", "has_courtyard", "garden", "has_garden"
        ),
        labels=("ეზო", "yard", "courtyard", "garden")
    )

    # ========================================================
    # RETURN SAME FORMAT AS SS.GE
    # ========================================================

    phone = extract_phones(
        [
            statement.get("user_phone_number"),
            statement.get("additional_phone_number"),
        ]
    ) or extract_phones(
        first_value(
            statement,
            "phones",
            "phone_numbers",
            "phone",
            "phone_number",
            "mobile"
        )
    ) or extract_myhome_phone_from_page(soup)

    # Asterisks mean this is the protected, pre-click value. Try the browser
    # reveal only for MyHome URLs, then retain the mask if revealing fails.
    if url and (not phone or "*" in phone):
        phone = reveal_myhome_phone(url) or phone

    return {

        "ბინის ID": listing_id,

        "მესაკუთრის ID": first_value(
            statement, "owner_id", "ownerId", "user_id", "userId", "contact_id"
        ),

        "მესაკუთრის ნომერი": phone,

        "ოთახები და საძინებელი": rooms_and_bedrooms(rooms, bedrooms),

        "კვადრატულობა": square_meters(area),

        "აგენტის სახელი": agent_name,

        "მესაკუთრის სახელი": owner_name,

        "ტელეფონის ნომერი": phone,

        "განცხადების სახეობა": deal_type,

        "ქონების ტიპი": property_type,

        "ქალაქი": city,

        "მისამართი": address,

        "უბანი": district,

        "ოთახები": rooms,

        "საძინებელი": bedrooms,

        "ფართობი (მ²)": area,

        "სართული": floor,

        "სართულიანობა": total_floors,

        "ფასი": price,

        "ვალუტა": currency,

        "ფასი / მ²": price_m2,

        "მდგომარეობა": first_value(
            statement,
            "condition",
            "condition_name",
            "state"
        ),

        "სტატუსი": building_status,

        "გაყიდვა/ქირა": deal_type,

        "აგენტი": poster_type,

        "რემონტი": first_value(
            statement,
            "repair",
            "repair_type",
            "renovation",
            "condition"
        ),

        "ლიფტი": elevator,

        "პარკინგი": first_value(
            statement,
            "parking",
            "has_parking",
            "parking_type_id"
        ),

        "ეზო": yard,

        "ფოტო": len(images),

        "მიღების თარიღი": datetime.now().strftime("%Y-%m-%d"),

        "კომენტარი": first_value(
            statement,
            "comment",
            "description"
        ),

        "images": images
    }


# ============================================================
# MEPATRONE.COM
# ============================================================

def mepatrone_detail_values(soup):
    """Reads the visible label/value rows from a Mepatrone listing."""

    details = {}
    for row in soup.find_all("div"):
        label = row.find("dt")
        value = row.find("dd")
        if label and value:
            key = clean(label.get_text(" ", strip=True))
            if key and key not in details:
                details[key] = clean(value.get_text(" ", strip=True))
    return details


def parse_mepatrone(html, url):
    """Parses a public Mepatrone listing into the common scraper format."""

    soup = BeautifulSoup(html, "html.parser")
    match = re.search(r"/listing/(\d+)", urlparse(url).path)
    if not match:
        raise Exception("Mepatrone განცხადების ID ვერ მოიძებნა")
    listing_id = match.group(1)
    details = mepatrone_detail_values(soup)

    title = soup.find("h1")
    title_text = clean(title.get_text(" ", strip=True)) if title else ""
    deal_type = next(
        (
            clean(tag.get_text(" ", strip=True))
            for tag in soup.find_all(("span", "p"))
            if clean(tag.get_text(" ", strip=True)) in {"იყიდება", "ქირავდება"}
        ),
        None,
    )
    property_type = "ბინა" if "ბინა" in title_text else None

    price_text = next(
        (
            clean(tag.get_text(" ", strip=True))
            for tag in soup.find_all("p")
            if re.fullmatch(r"(?:\$|₾)\s?[\d,.]+", clean(tag.get_text(" ", strip=True)))
        ),
        "",
    )
    # Mepatrone uses commas as thousands separators (for example $230,000),
    # unlike the decimal comma handled by the general ``number`` helper.
    price_digits = re.sub(r"[^\d]", "", price_text)
    price = float(price_digits) if price_digits else None
    currency = "USD" if "$" in price_text else ("GEL" if "₾" in price_text else None)

    rooms_text = details.get("ოთახები")
    rooms_match = re.search(r"\d+", rooms_text or "")
    rooms = rooms_match.group(0) if rooms_match else None
    area_text = details.get("ფართი")
    area = number(area_text)
    floor_text = details.get("სართული")
    floor_match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", floor_text or "")
    floor = floor_match.group(1) if floor_match else floor_text or None
    total_floors = floor_match.group(2) if floor_match else None

    images = []
    image_prefix = f"/img/{listing_id}/"
    for tag in soup.find_all("img", src=True):
        src = clean(tag["src"])
        if src.startswith(image_prefix):
            images.append(urljoin(url, src))
    images = list(dict.fromkeys(images))

    phone_pattern = re.compile(r"(?:\+995\s*)?5\d{2}[\s-]*\d{3}[\s-]*\d{3}")
    phone_match = phone_pattern.search(soup.get_text(" ", strip=True))
    phone = phone_match.group(0) if phone_match else None

    description_heading = soup.find(
        lambda tag: tag.name in ("h2", "h3")
        and clean(tag.get_text(" ", strip=True)) == "აღწერა"
    )
    description = None
    if description_heading:
        description_tag = description_heading.find_next("p")
        if description_tag:
            description = clean(description_tag.get_text("\n", strip=True))

    return {
        "ბინის ID": listing_id,
        "მესაკუთრის ID": None,
        "მესაკუთრის ნომერი": phone,
        "ოთახები და საძინებელი": rooms_and_bedrooms(rooms, None),
        "კვადრატულობა": square_meters(area),
        "აგენტის სახელი": None,
        "მესაკუთრის სახელი": None,
        "ტელეფონის ნომერი": phone,
        "განცხადების სახეობა": deal_type,
        "ქონების ტიპი": property_type,
        "ქალაქი": "თბილისი",
        "მისამართი": details.get("მდებარეობა"),
        "უბანი": details.get("უბანი"),
        "ოთახები": rooms,
        "საძინებელი": None,
        "ფართობი (მ²)": area,
        "სართული": floor,
        "სართულიანობა": total_floors,
        "ფასი": price,
        "ვალუტა": currency,
        "ფასი / მ²": round(price / area, 2) if price and area else None,
        "მდგომარეობა": details.get("მდგომარეობა"),
        "სტატუსი": details.get("შენობის სტატუსი"),
        "გაყიდვა/ქირა": deal_type,
        "აგენტი": "მესაკუთრე",
        "რემონტი": details.get("მდგომარეობა"),
        "ლიფტი": None,
        "პარკინგი": None,
        "ეზო": None,
        "ფოტო": len(images),
        "მიღების თარიღი": datetime.now().strftime("%Y-%m-%d"),
        "კომენტარი": description,
        "images": images,
    }


# ============================================================
# ROUTER
# ============================================================

def get_listing(url):

    print("\nგანცხადების მონაცემები იტვირთება...")
    html = fetch_html(url)

    if "ss.ge" in url.lower():
        listing = parse_ss_ge(html, url)
        source = "ss.ge"

    elif "myhome.ge" in url.lower():
        listing = parse_myhome(html, url)
        source = "myhome"

    elif "mepatrone.com" in url.lower():
        listing = parse_mepatrone(html, url)
        source = "mepatrone.com"

    else:
        raise Exception("Unsupported website")

    listing["წყარო"] = source
    listing["ბმული"] = url
    return listing


# ============================================================
# PRINT RESULT
# ============================================================

def print_listing(l):

    print("\n============================")
    print("SCRAPED LISTING")
    print("============================")

    for key, value in l.items():

        if key == "images":
            continue

        print(f"{key}: {value}")

    print("============================")


# ============================================================
# SAVE JSON
# ============================================================

def image_extension(url, content_type):
    """Chooses a safe image file extension from a URL or response header."""

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix

    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(clean(content_type).split(";", 1)[0].lower(), ".jpg")


def save_images(listing, folder):
    """Downloads listing photos to ``Images`` without stopping a scrape."""

    images_folder = folder / "Images"
    images_folder.mkdir(exist_ok=True)
    image_urls = list(dict.fromkeys(clean(url) for url in listing.get("images", []) if clean(url)))
    downloaded = 0

    for index, url in enumerate(image_urls, start=1):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            response.raise_for_status()
            if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                continue

            extension = image_extension(url, response.headers.get("Content-Type"))
            image_path = images_folder / f"{index:02d}{extension}"
            image_path.write_bytes(response.content)
            downloaded += 1
        except requests.RequestException as error:
            print(f"ფოტო {index} ვერ ჩამოიტვირთა: {error}")

    print(f"ფოტოები: {downloaded}/{len(image_urls)} შენახულია: {images_folder}")


def save_json(l):

    desktop = Path.home() / "Desktop"

    base = desktop / "MyHome Listings"
    base.mkdir(exist_ok=True)

    listing_id = l.get("ბინის ID") or "unknown"

    folder = base / str(listing_id)
    folder.mkdir(exist_ok=True)

    with open(
        folder / "data.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            l,
            f,
            ensure_ascii=False,
            indent=4
        )

    save_images(l, folder)
    print(f"\nშენახულია: {folder}")


def find_excel_database():
    """Returns the Desktop workbook named ``ბაზა``.

    This is intentionally an exact filename match: the scraper must not pick
    up an older workbook from Downloads or a temporary ``~$`` Excel lock file.
    """

    # Windows may redirect Desktop into OneDrive. Search the usual locations
    # while preserving the exact required workbook name.
    desktop_locations = [Path.home() / "Desktop"]
    for one_drive_variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        one_drive = os.environ.get(one_drive_variable)
        if one_drive:
            desktop_locations.append(Path(one_drive) / "Desktop")

    for desktop in dict.fromkeys(desktop_locations):
        for extension in (".xlsx", ".xlsm", ".xls"):
            workbook = desktop / f"ბაზა{extension}"
            if workbook.is_file() and not workbook.name.startswith("~$"):
                return workbook

    raise FileNotFoundError(
        "Desktop-ზე ვერ მოიძებნა Excel ფაილი სახელით „ბაზა.xlsx“"
    )


def find_open_excel_workbook(workbook_path):
    """Returns ``(Excel.Application, Workbook)`` for an already-open file.

    ``GetActiveObject`` only exposes one Excel process.  A user can have
    several Excel processes, so locate the native workbook window as well;
    this makes it possible to write to ბაზა.xlsx while it stays open.
    """

    try:
        import pythoncom
        import win32com.client
        import win32gui
        from win32com.client.gencache import EnsureModule

        EnsureModule("{00020813-0000-0000-C000-000000000046}", 0, 1, 9)
    except Exception:
        return None, None

    target_path = os.path.normcase(os.path.abspath(str(workbook_path)))
    excel_windows = []

    def collect_excel_windows(hwnd, _):
        if win32gui.GetClassName(hwnd) == "XLMAIN":
            excel_windows.append(hwnd)

    try:
        win32gui.EnumWindows(collect_excel_windows, None)
        for main_window in excel_windows:
            desktop_window = win32gui.FindWindowEx(
                main_window, 0, "XLDESK", None
            )
            document_window = win32gui.FindWindowEx(
                desktop_window, 0, "EXCEL7", None
            )
            if not document_window:
                continue

            raw_window = pythoncom.ObjectFromLresult(
                win32gui.SendMessage(document_window, 0x003D, 0, -16),
                pythoncom.IID_IDispatch,
                0,
            )
            excel_window = win32com.client.Dispatch(
                raw_window,
                userName="Excel.Window",
                resultCLSID="{00020893-0000-0000-C000-000000000046}",
            )
            excel = excel_window.Application

            for index in range(1, excel.Workbooks.Count + 1):
                workbook = excel.Workbooks.Item(index)
                open_path = os.path.normcase(
                    os.path.abspath(str(workbook.FullName))
                )
                if open_path == target_path:
                    return excel, workbook
    except Exception:
        pass

    return None, None


def excel_column_letter(column):
    """Converts a 1-based Excel column number to its letter form."""

    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def excel_cell(worksheet, row, column):
    """Returns a cell through Range, compatible with attached Excel windows."""

    return worksheet.Range(f"{excel_column_letter(column)}{row}")


EXCEL_COLUMN_ALIASES = {
    # ბაზა.xlsx column layout.  These are the fields marked „ავტო“ in row 2.
    "ბინის ID": ("მესაკუთრის ID",),
    "მესაკუთრის ნომერი": ("მესაკუთრის ნომერი",),
    "უბანი": ("უბანი",),
    "ოთახები და საძინებელი": ("ოთახები და საძინებელი",),
    "კვადრატულობა": ("კვადრატულობა",),
    "აგენტის სახელი": ("აგენტის სახელი",),
    "მესაკუთრის სახელი": ("მესაკუთრის სახელი",),
    "განცხადების სახეობა": ("გარიგების ტიპი",),
    "საძინებელი": ("საძინებლები",),
    "ფართობი (მ²)": ("ფართობი", "ფართობი / მ²"),
    "ფოტო": ("ფოტოების რაოდენობა",),
}

# These columns are marked „ხელი“ in ბაზა.xlsx.  Even though a listing page
# may expose a price, the workbook owner enters these values themselves.
MANUAL_EXCEL_COLUMNS = {
    "ფასი",
    "ჩემი ID MYHOME",
    "ჩემი ID SS.GE",
    "კომპლექსი/მშენებლობა",
}


def save_to_excel(listing):
    """Adds or updates a listing without damaging Excel Table metadata."""

    try:
        import win32com.client
    except ImportError:
        print(
            "\nExcel მოდული EXE-ში არ არის ჩაშენებული. "
            "ხელახლა ააწყვე main.spec-ით და დაყენებული pywin32-ით."
        )
        return

    excel = None
    workbook = None
    saved = False
    using_open_workbook = False

    try:
        workbook_path = find_excel_database()
        print(f"\nExcel ფაილი: {workbook_path}")
        normalized_path = os.path.normcase(os.path.abspath(str(workbook_path)))

        # Reuse the workbook already open in the user's Excel window. This
        # also handles a database opened in a different Excel process.
        excel, workbook = find_open_excel_workbook(workbook_path)
        using_open_workbook = workbook is not None

        # A simpler fallback covers normal single-instance Excel sessions.
        try:
            if workbook is None:
                open_excel = win32com.client.GetActiveObject("Excel.Application")
                for open_workbook in open_excel.Workbooks:
                    open_path = os.path.normcase(os.path.abspath(str(open_workbook.FullName)))
                    if open_path == normalized_path:
                        excel = open_excel
                        workbook = open_workbook
                        using_open_workbook = True
                        break
        except Exception:
            pass

        if workbook is None:
            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            try:
                workbook = excel.Workbooks.Open(
                    Filename=str(workbook_path),
                    UpdateLinks=0,
                    ReadOnly=False,
                    IgnoreReadOnlyRecommended=True,
                    CorruptLoad=1,
                )
            except Exception:
                # Downloads marked as coming from the internet open in Excel's
                # Protected View, where Workbooks.Open can refuse edit access.
                protected_view = excel.ProtectedViewWindows.Open(str(workbook_path))
                workbook = protected_view.Edit()

        # When ბაზა.xlsx is already open in the user's Excel session, this
        # separate Excel process can receive a read-only/temporary copy.  A
        # write there disappears on close, so do not claim success: ask the
        # user to close the workbook and run the scraper again.
        if workbook.ReadOnly:
            raise PermissionError(
                "Excel ფაილი გახსნილია ან ჩაკეტილია. დახურე „ბაზა.xlsx“ და თავიდან გაუშვი სკრეპერი."
            )

        # The database uses its first sheet (currently Sheet1).  Do not use
        # ActiveSheet: when several Excel windows are open it may point to a
        # different workbook, or be unavailable through COM automation.
        raw_worksheet = workbook.Worksheets.Item(1)
        worksheet = win32com.client.Dispatch(
            raw_worksheet._oleobj_,
            userName="Excel.Worksheet",
            resultCLSID="{00020820-0000-0000-C000-000000000046}",
        )

        # Do not read ``worksheet.Name`` here.  In some already-open Excel
        # instances COM returns the worksheet as a generic ``Item`` object;
        # its Name property can fail even though Cells and Values work.  The
        # database sheet is always the first worksheet.
        print("Excel ფურცელი: პირველი ფურცელი")

        # The database has a small fixed header row.  Reading through Range
        # (instead of Cells) works when it is already open in another Excel
        # process.
        last_column = 1
        for column in range(1, 101):
            if clean(excel_cell(worksheet, 1, column).Value):
                last_column = column
        headers = {
            normalize_field_name(excel_cell(worksheet, 1, column).Value): column
            for column in range(1, last_column + 1)
            if clean(excel_cell(worksheet, 1, column).Value)
        }

        # Column A keeps the workbook's existing visible heading, while the
        # apartment/listing ID is written into it.
        first_auto_column = headers.get(normalize_field_name("მესაკუთრის ID"), 1)
        last_row = 1
        while clean(excel_cell(worksheet, last_row + 1, first_auto_column).Value):
            last_row += 1
        target_row = max(last_row + 1, 2)

        print(f"Excel რიგი: {target_row}")

        for key, value in listing.items():
            # Column A is visibly named "მესაკუთრის ID" in the workbook for
            # compatibility, but it must always contain "ბინის ID".  Do not
            # let the website account/owner ID overwrite the listing ID.
            if key in {"images", "მესაკუთრის ID"} or key in MANUAL_EXCEL_COLUMNS:
                continue

            possible_headers = (key,) + EXCEL_COLUMN_ALIASES.get(key, ())
            column = next(
                (
                    headers.get(normalize_field_name(header))
                    for header in possible_headers
                    if headers.get(normalize_field_name(header))
                ),
                None
            )
            if column:
                cell = excel_cell(worksheet, target_row, column)
                # Value2 avoids Excel's locale/date coercion in an attached
                # workbook.  Prefixing with an apostrophe stores a display
                # string (the apostrophe is not shown in the cell), preserving
                # phone numbers and values such as "2 ოთახი 1 საძ".
                text_value = clean(value) if value is not None else ""
                cell.Value2 = f"'{text_value}" if text_value else ""

        workbook.Save()
        saved = True
        if not workbook.Saved:
            raise OSError("Excel ფაილის შენახვა ვერ დადასტურდა")
        saved_id = excel_cell(worksheet, target_row, first_auto_column).Value
        print(f"Excel-ში შემოწმებული ID: {saved_id}")
        print(f"Excel ბაზაში დამატებულია: {workbook_path}")
    except Exception as error:
        print(f"\nExcel-ში შენახვა ვერ მოხერხდა: {type(error).__name__}: {error}")
        print(
            "შეამოწმე, რომ Microsoft Excel დაყენებულია, ბაზა.xlsx არის "
            "Desktop-ზე და ფაილი არ არის Protected View-ში."
        )
    finally:
        if workbook and not using_open_workbook:
            workbook.Close(SaveChanges=saved)
        if excel and not using_open_workbook:
            excel.Quit()


# ============================================================
# MAIN
# ============================================================

def main():
    while True:
        url = input(
            "ჩაწერე SS.ge ან MyHome.ge ლინკი (გასასვლელად: exit):\n"
        ).strip()

        if url.casefold() in {"exit", "გასვლა", "q", "quit"}:
            break

        if not url:
            continue

        try:
            listing = get_listing(url)
            print_listing(listing)
            save_json(listing)
            save_to_excel(listing)
        except Exception as error:
            print("\nშეცდომა:")
            print(error)

        print("\nშეგიძლია ჩაწერო შემდეგი ლინკი.")


if __name__ == "__main__":
    try:
        main()
    finally:
        # When the packaged app is opened by double-clicking, Windows closes
        # its console as soon as the program ends. Keep it visible so errors
        # and results can be read. This does not affect normal ``python
        # main.py`` runs in a terminal.
        if getattr(sys, "frozen", False):
            try:
                input("\nგასასვლელად დააჭირე Enter-ს...")
            except EOFError:
                pass
