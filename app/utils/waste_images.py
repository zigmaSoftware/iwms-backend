"""Helpers to surface waste-collection capture photos.

The photos are taken in the mobile capture flow and stored on
``WasteCollectionSub.image``. They are linked to a household collection only by
customer + date (there is no direct FK), so these helpers resolve and build
servable ``/media/`` URLs for the DailyTripLog serializer.
"""

import datetime as _dt

from django.conf import settings
from django.utils import timezone as _tz


def build_media_url(image, request=None):
    """Build a servable media URL from a stored image path.

    The mobile uploader stores paths like ``uploads/waste_collection_images/<f>``
    even though the file is served from ``MEDIA_URL + 'waste_collection_images/'``,
    so rebuild the URL from the filename to avoid the stale ``uploads/`` prefix.
    """
    if not image:
        return None
    image = str(image)
    if image.startswith("http"):
        return image
    filename = image.rstrip("/").split("/")[-1]
    path = f"{settings.MEDIA_URL}waste_collection_images/{filename}"
    return request.build_absolute_uri(path) if request is not None else path


def _capture_window(collection_date):
    """The collection day ± 1 day, to absorb timezone skew (a ``__date``
    lookup is unreliable on MySQL without the tz tables loaded)."""
    start = _dt.datetime.combine(collection_date, _dt.time.min) - _dt.timedelta(days=1)
    end = _dt.datetime.combine(collection_date, _dt.time.max) + _dt.timedelta(days=1)
    if _tz.is_aware(_tz.now()):
        start = _tz.make_aware(start)
        end = _tz.make_aware(end)
    return start, end


def _capture_subs(customer_ids):
    from app.models.waste_collection_bluetooth.waste_collection_bluetooth import WasteCollectionSub

    return (
        WasteCollectionSub.objects.filter(customer_id__in=customer_ids, is_deleted=False)
        .exclude(image__isnull=True)
        .exclude(image="")
    )


def _image_rows(subs, request):
    images = []
    for sub in subs:
        url = build_media_url(sub.image, request)
        if url:
            images.append({
                "url": url,
                "waste_type_id": sub.waste_type_id,
                "weight": sub.weight,
            })
    return images


def capture_images_for_customer(customer_id, collection_date=None, request=None):
    """Capture photos (``WasteCollectionSub``) for a household, linked by customer
    and — when supplied — the collection day (see ``_capture_window``)."""
    if not customer_id:
        return []

    subs = _capture_subs([customer_id])
    if collection_date:
        start, end = _capture_window(collection_date)
        subs = subs.filter(date_time__gte=start, date_time__lte=end)
    return _image_rows(subs.order_by("date_time"), request)


def capture_images_for_customers(pairs, request=None):
    """Batch form of ``capture_images_for_customer``: one query for every
    ``(customer_id, collection_date)`` pair, returning a list per pair."""
    customer_ids = {customer_id for customer_id, _ in pairs if customer_id}
    by_customer = {}
    if customer_ids:
        for sub in _capture_subs(customer_ids).order_by("date_time"):
            by_customer.setdefault(sub.customer_id, []).append(sub)

    results = []
    for customer_id, collection_date in pairs:
        subs = by_customer.get(customer_id, []) if customer_id else []
        if collection_date:
            start, end = _capture_window(collection_date)
            subs = [sub for sub in subs if start <= sub.date_time <= end]
        results.append(_image_rows(subs, request))
    return results


# Waste fields on WasteCollection that can carry a per-waste-type photo from
# the web Household Collections form. Stored as WasteCollectionSub rows with
# screen_unique_id = WasteCollection.unique_id and form_unique_id = "web:<field>".
WEB_WASTE_IMAGE_FIELDS = ("wet_waste", "dry_waste", "mixed_waste", "sanitary_waste")

WEB_WASTE_IMAGE_LABELS = {
    "wet_waste": "Wet Waste",
    "dry_waste": "Dry Waste",
    "mixed_waste": "Mixed Waste",
    "sanitary_waste": "Sanitary Waste",
}


def web_waste_images_for_collections(collection_ids, request=None):
    """Per-waste-type photos uploaded from the web form, keyed by
    WasteCollection.unique_id (one query for all ids)."""
    from app.models.waste_collection_bluetooth.waste_collection_bluetooth import WasteCollectionSub

    ids = [cid for cid in collection_ids if cid]
    result = {}
    if not ids:
        return result
    subs = (
        WasteCollectionSub.objects.filter(
            screen_unique_id__in=ids,
            form_unique_id__startswith="web:",
            is_deleted=False,
        )
        .exclude(image__isnull=True)
        .exclude(image="")
        .order_by("date_time")
    )
    for sub in subs:
        url = build_media_url(sub.image, request)
        if not url:
            continue
        field = sub.form_unique_id.split(":", 1)[1]
        result.setdefault(sub.screen_unique_id, []).append({
            "url": url,
            "field": field,
            "waste_type_id": sub.waste_type_id,
            "waste_type_name": WEB_WASTE_IMAGE_LABELS.get(field, field),
        })
    return result


def _waste_type_names(waste_type_ids):
    from app.models.waste_collection_bluetooth.waste_collection_bluetooth import WasteType

    ids = {wid for wid in waste_type_ids if wid}
    if not ids:
        return {}
    return dict(
        WasteType.objects.filter(unique_id__in=ids).values_list("unique_id", "waste_type_name")
    )


def _file_url(image_field, request=None):
    """Same as serializers.evidence_image.absolute_image_url, without a serializer."""
    if not image_field:
        return None
    try:
        url = image_field.url
    except (ValueError, AttributeError):
        return None
    return request.build_absolute_uri(url) if request is not None else url


def household_row_images(households, request=None):
    """Every photo for each DailyTripHouseholdCollection row, labelled with
    its waste type, keyed by the row's unique_id.

    Order: per-waste-type photos from the web form, then mobile capture
    photos (customer + collection day), then the single photo stored on the
    WasteCollection (only when there are no per-waste photos — the mobile
    bridge copies one of its own photos there), then status evidence.
    """
    from app.models.core_modules.daily_operations.wastecollection import WasteCollection

    households = list(households)
    waste_ids = [hh.waste_collection_id for hh in households if getattr(hh, "waste_collection_id", None)]
    wastes = {
        w.unique_id: w
        for w in WasteCollection.objects.filter(unique_id__in=waste_ids)
    } if waste_ids else {}

    web_by_waste = web_waste_images_for_collections(list(wastes.keys()), request)

    pairs = []
    for hh in households:
        waste = wastes.get(getattr(hh, "waste_collection_id", None))
        day = getattr(waste, "collection_date", None)
        if day is None and getattr(hh, "collected_at", None):
            day = hh.collected_at.date()
        pairs.append((hh.customer_id, day) if day else (None, None))
    mobile_lists = capture_images_for_customers(pairs, request)

    names = _waste_type_names(
        img.get("waste_type_id") for images in mobile_lists for img in images
    )

    result = {}
    for hh, mobile in zip(households, mobile_lists):
        waste = wastes.get(getattr(hh, "waste_collection_id", None))
        base = {
            "household_collection_id": hh.unique_id,
            "trip_household_collection_id": hh.unique_id,
        }
        images, seen = [], set()

        def add(img):
            if img["url"] and img["url"] not in seen:
                seen.add(img["url"])
                images.append({**img, **base})

        for img in web_by_waste.get(getattr(waste, "unique_id", None), []):
            add({**img, "image_type": "collection"})
        for img in mobile:
            add({
                **img,
                "waste_type_name": names.get(img.get("waste_type_id")),
                "image_type": "collection",
            })
        if not images and waste is not None:
            url = build_media_url(getattr(waste, "image", None), request)
            if url:
                add({"url": url, "waste_type_name": None, "image_type": "collection"})
        evidence_url = _file_url(getattr(hh, "status_evidence_image", None), request)
        if evidence_url:
            add({"url": evidence_url, "waste_type_name": None, "image_type": "status_evidence"})
        result[hh.unique_id] = images
    return result
