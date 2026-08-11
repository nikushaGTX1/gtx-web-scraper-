import requests
from bs4 import BeautifulSoup
import json
from pathlib import Path
import re
import sys
from datetime import datetime


# A frozen Windows executable can otherwise inherit a legacy console codec
# (for example cp1252), which cannot print the Georgian prompts and labels.
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

def parse_ss_ge(html):

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

    district = first_value(
        addr,
        "districtTitle",
        "subdistrictTitle",
        "municipalityTitle"
    )

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

def parse_myhome(html):

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

    district = first_value(
        statement,
        "district_name",
        "district",
        "region_name"
    )

    # These properties are not consistently flat fields in MyHome data.
    # Newer listings often keep them in nested, labelled attribute lists.
    agent = find_myhome_attribute(
        statement,
        keys=(
            "agent", "agent_name", "agency", "agency_name", "agency_info",
            "company", "company_name", "broker", "broker_name", "realtor",
            "realtor_name", "owner_type", "user_type"
        ),
        labels=("აგენტი", "სააგენტო", "რელტორი", "agent", "agency", "broker", "realtor")
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

    return {

        "ბინის ID": listing_id,

        "აგენტის სახელი": first_value(
            statement,
            "owner_name",
            "agent_name",
            "contact_name",
            "user_name"
        ),

        "ტელეფონის ნომერი": extract_phones(
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
        ),

        "განცხადების სახეობა": first_value(
            statement,
            "statement_type",
            "listing_type",
            "deal_type_id"
        ),

        "ქონების ტიპი": first_value(
            statement,
            "property_type",
            "property_type_name",
            "real_estate_type_id"
        ),

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

        "სტატუსი": first_value(
            statement,
            "status",
            "status_name",
            "status_id"
        ),

        "გაყიდვა/ქირა": first_value(
            statement,
            "deal_type",
            "transaction_type",
            "operation_type",
            "deal_type_id"
        ),

        "აგენტი": agent,

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
# ROUTER
# ============================================================

def get_listing(url):

    html = fetch_html(url)

    if "ss.ge" in url.lower():
        return parse_ss_ge(html)

    if "myhome.ge" in url.lower():
        return parse_myhome(html)

    raise Exception("Unsupported website")


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

    print(f"\nშენახულია: {folder}")


# ============================================================
# MAIN
# ============================================================

def main():

    url = input(
        "ჩაწერე SS.ge ან MyHome.ge ლინკი:\n"
    ).strip()

    try:

        listing = get_listing(url)

        print_listing(listing)

        save_json(listing)

    except Exception as e:

        print("\nშეცდომა:")
        print(e)


if __name__ == "__main__":
    main()
