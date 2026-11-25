from math import asin, cos, radians, sin, sqrt

from rest_framework import serializers
from app.serializers.company_projects.tenancy import TenancyReadSerializerMixin
from app.models.core_modules.daily_operations.wastecollection import WasteCollection
from app.models.masters.customer_masters.customercreation import CustomerCreation
from app.models.masters.ward import Ward
from app.models.masters.zone import Zone
from app.models.masters.city import City
from app.models.masters.district import District
from app.models.masters.panchayat import Panchayat
from app.models.superadmin.common_masters.state import State
from app.models.superadmin.common_masters.country import Country
from app.models.superadmin_masters.company import Company
from app.models.superadmin_masters.project import Project
from app.models.core_modules.daily_operations.daily_trip_assignment import DailyTripAssignment
from app.utils.name_or_id_field import NameOrUniqueIdField
from app.serializers.superadmin.staff_management.user_serializer import UniqueIdOrPkField
from app.utils.related_prefetch import cached_related, prefetch_related_rows, related
from app.utils.waste_images import (
    WEB_WASTE_IMAGE_FIELDS,
    build_media_url,
    web_waste_images_for_collections,
)

RELATED_LOOKUPS = {
    "company": (Company, "company_id"),
    "project": (Project, "project_id"),
    "customer": (CustomerCreation, "customer_id"),
    "ward": (Ward, "ward_id"),
}

CUSTOMER_LOOKUPS = {
    "zone": (Zone, "zone_id"),
    "city": (City, "city_id"),
    "district": (District, "district_id"),
    "state": (State, "state_id"),
    "country": (Country, "country_id"),
    "panchayat": (Panchayat, "panchayat_id"),
}


class WasteCollectionListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        instances = list(data.all() if hasattr(data, "all") else data)
        rows = prefetch_related_rows(instances, RELATED_LOOKUPS)
        prefetch_related_rows(list(rows["customer"].values()), CUSTOMER_LOOKUPS)
        return super().to_representation(instances)


class CustomerField(serializers.Field):
    """Accept customer unique_id, write into `customer_id` (a plain CharField
    on WasteCollection — `customer` itself is a read-only @property), and
    serialize back the unique_id."""

    def __init__(self, *args, queryset=None, **kwargs):
        self._queryset = queryset
        super().__init__(*args, **kwargs)

    def get_queryset(self):
        return self._queryset

    def to_representation(self, value):
        # `value` is `instance.customer_id` (a plain string) since this
        # field's `source` is set to `customer_id` on the serializer.
        return value or None

    def to_internal_value(self, data):
        value = str(data).strip() if data is not None else ""
        if not value:
            raise serializers.ValidationError("Customer is required")
        queryset = self.get_queryset()
        if queryset is None:
            raise serializers.ValidationError("Invalid customer reference")
        obj = queryset.filter(unique_id=value).first()
        if obj:
            return obj.unique_id
        raise serializers.ValidationError("Invalid customer reference")


class WasteCollectionSerializer(TenancyReadSerializerMixin, serializers.ModelSerializer):
    MAX_COLLECTION_DISTANCE_METERS = 10
    # `customer` is now a read-only @property on the model (looked up from the
    # plain `customer_id` CharField), so writes target `customer_id` directly.
    customer = CustomerField(
        source="customer_id",
        queryset=CustomerCreation.objects.filter(is_deleted=False),
        write_only=True,
    )
    # Expose customer unique identifier as `customer_id`
    customer_id = serializers.CharField(read_only=True)
    # `ward_id` is now a plain CharField on WasteCollection (auto-inherited
    # from the household via copy_flat_geo when left blank, see the model's
    # save()), but selectable/editable so a collection can be scoped
    # independently.
    ward_id = NameOrUniqueIdField(
        name_field="ward_name",
        queryset=Ward.objects.filter(is_deleted=False),
        required=False, allow_null=True,
    )
    ward_name = serializers.SerializerMethodField()
    zone_name = serializers.SerializerMethodField()
    city_name = serializers.SerializerMethodField()
    district_name = serializers.SerializerMethodField()
    state_name = serializers.SerializerMethodField()
    country_name = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    panchayat_name = serializers.SerializerMethodField()

    trip_assignment_id = UniqueIdOrPkField(
        queryset=DailyTripAssignment.objects.filter(is_deleted=False),
        required=False,
        allow_null=True,
    )
    trip_assignment_display = serializers.CharField(
        source="trip_assignment_id", read_only=True, default=None
    )

    # Collection photo uploaded from the web form. Saved to the same folder
    # as the mobile capture photos, and its path stored on `image`.
    image_file = serializers.ImageField(write_only=True, required=False, allow_null=True)
    image_url = serializers.SerializerMethodField()

    # One photo per waste type, like the mobile app. Each is stored as a
    # WasteCollectionSub row (screen_unique_id = this collection) so every
    # screen that reads mobile capture photos picks it up too.
    wet_waste_image = serializers.ImageField(write_only=True, required=False, allow_null=True)
    dry_waste_image = serializers.ImageField(write_only=True, required=False, allow_null=True)
    mixed_waste_image = serializers.ImageField(write_only=True, required=False, allow_null=True)
    sanitary_waste_image = serializers.ImageField(write_only=True, required=False, allow_null=True)
    wet_waste_type_id = serializers.CharField(write_only=True, required=False, allow_blank=True)
    dry_waste_type_id = serializers.CharField(write_only=True, required=False, allow_blank=True)
    mixed_waste_type_id = serializers.CharField(write_only=True, required=False, allow_blank=True)
    sanitary_waste_type_id = serializers.CharField(write_only=True, required=False, allow_blank=True)
    waste_images = serializers.SerializerMethodField()

    class Meta:
        model = WasteCollection
        fields = "__all__"
        read_only_fields = ["unique_id", "total_quantity", "collection_time"]
        extra_kwargs = {
            "customer": {"write_only": True},
        }
        list_serializer_class = WasteCollectionListSerializer

    def get_image_url(self, obj):
        return build_media_url(obj.image, self.context.get("request"))

    def get_waste_images(self, obj):
        # Detail only: avoids one extra query per row on the list endpoint.
        if isinstance(self.parent, serializers.ListSerializer):
            return []
        return web_waste_images_for_collections(
            [obj.unique_id], self.context.get("request")
        ).get(obj.unique_id, [])

    def _store_image_file(self, validated_data):
        image_file = validated_data.pop("image_file", None)
        if image_file:
            from app.models.waste_collection_bluetooth.waste_collection_bluetooth import upload_image
            validated_data["image"] = upload_image(image_file)
        return validated_data

    def _pop_waste_images(self, validated_data):
        images = {}
        for field in WEB_WASTE_IMAGE_FIELDS:
            image = validated_data.pop(f"{field}_image", None)
            waste_type_id = (validated_data.pop(f"{field}_type_id", "") or "").strip()
            if image:
                images[field] = (image, waste_type_id)
        return images

    def _resolve_waste_type_id(self, instance, field, waste_type_id):
        from app.models.waste_collection_bluetooth.waste_collection_bluetooth import WasteType

        if waste_type_id and WasteType.objects.filter(unique_id=waste_type_id).exists():
            return waste_type_id
        keyword = {"wet_waste": "wet", "dry_waste": "dry", "mixed_waste": "mix",
                   "sanitary_waste": "sanitary"}[field]
        queryset = WasteType.objects.filter(is_deleted=False, waste_type_name__icontains=keyword)
        match = (
            queryset.filter(company_id=instance.company_id, project_id=instance.project_id).first()
            or queryset.first()
        )
        return match.unique_id if match else field

    def _save_waste_images(self, instance, images):
        if not images:
            return
        from app.models.waste_collection_bluetooth.waste_collection_bluetooth import (
            WasteCollectionSub,
            upload_image,
        )

        for field, (image, waste_type_id) in images.items():
            form_unique_id = f"web:{field}"
            # Replacing a photo: retire the previous one for this waste type.
            WasteCollectionSub.objects.filter(
                screen_unique_id=instance.unique_id,
                form_unique_id=form_unique_id,
                is_deleted=False,
            ).update(is_deleted=True, delete_reason="Replaced from web form")
            WasteCollectionSub.objects.create(
                company_id=instance.company_id,
                project_id=instance.project_id,
                screen_unique_id=instance.unique_id,
                form_unique_id=form_unique_id,
                customer_id=instance.customer_id or "",
                waste_type_id=self._resolve_waste_type_id(instance, field, waste_type_id),
                image=upload_image(image),
                # Weight stays on WasteCollection; 0 here keeps sub-based
                # weight totals from counting the same kilos twice.
                weight=0,
                latitude=str(instance.latitude) if instance.latitude is not None else None,
                longitude=str(instance.longitude) if instance.longitude is not None else None,
            )

    def create(self, validated_data):
        images = self._pop_waste_images(validated_data)
        instance = super().create(self._store_image_file(validated_data))
        self._save_waste_images(instance, images)
        return instance

    def update(self, instance, validated_data):
        images = self._pop_waste_images(validated_data)
        instance = super().update(instance, self._store_image_file(validated_data))
        self._save_waste_images(instance, images)
        return instance

    def get_company_name(self, obj):
        if not isinstance(getattr(obj, "company_id", None), str):
            return super().get_company_name(obj)
        return getattr(related(obj, "company"), "name", None)

    def get_project_name(self, obj):
        if not isinstance(getattr(obj, "project_id", None), str):
            return super().get_project_name(obj)
        return getattr(related(obj, "project"), "name", None)

    def get_ward_name(self, obj):
        ward = related(obj, "ward")
        return ward.ward_name if ward else None

    def get_customer_name(self, obj):
        customer = cached_related(obj, "customer")
        return customer.customer_name if customer else None

    def get_zone_name(self, obj):
        customer = cached_related(obj, "customer")
        zone = related(customer, "zone") if customer else None
        return zone.zone_name if zone else None

    def get_city_name(self, obj):
        customer = cached_related(obj, "customer")
        city = related(customer, "city") if customer else None
        return city.name if city else None

    def get_district_name(self, obj):
        customer = cached_related(obj, "customer")
        district = related(customer, "district") if customer else None
        return district.name if district else None

    def get_state_name(self, obj):
        customer = cached_related(obj, "customer")
        state = related(customer, "state") if customer else None
        return state.name if state else None

    def get_country_name(self, obj):
        customer = cached_related(obj, "customer")
        country = related(customer, "country") if customer else None
        return country.name if country else None

    def get_panchayat_name(self, obj):
        customer = cached_related(obj, "customer")
        panchayat = related(customer, "panchayat") if customer else None
        return panchayat.panchayat_name if panchayat else None

    @staticmethod
    def _distance_in_meters(from_latitude, from_longitude, to_latitude, to_longitude):
        """Great-circle distance using the haversine formula."""
        earth_radius_m = 6_371_000
        latitude_delta = radians(float(to_latitude) - float(from_latitude))
        longitude_delta = radians(float(to_longitude) - float(from_longitude))
        origin_latitude = radians(float(from_latitude))
        destination_latitude = radians(float(to_latitude))
        haversine = (
            sin(latitude_delta / 2) ** 2
            + cos(origin_latitude)
            * cos(destination_latitude)
            * sin(longitude_delta / 2) ** 2
        )
        return 2 * earth_radius_m * asin(sqrt(haversine))

    def validate(self, attrs):
        attrs = super().validate(attrs)
        status = attrs.get("status", getattr(self.instance, "status", None))
        if status != WasteCollection.STATUS_COLLECTED:
            return attrs

        customer_id = attrs.get("customer_id", getattr(self.instance, "customer_id", None))
        latitude = attrs.get("latitude", getattr(self.instance, "latitude", None))
        longitude = attrs.get("longitude", getattr(self.instance, "longitude", None))

        customer = CustomerCreation.objects.filter(
            unique_id=customer_id, is_deleted=False
        ).first()
        if not customer:
            raise serializers.ValidationError({"customer": "A valid customer is required."})

        # Without QR customers are count-only placeholders with no address or
        # coordinates, so collection lat/long and the distance check don't apply.
        if getattr(customer, "is_without_qr", False):
            if latitude is not None and not -90 <= float(latitude) <= 90:
                raise serializers.ValidationError(
                    {"latitude": "Collection latitude must be between -90 and 90."}
                )
            if longitude is not None and not -180 <= float(longitude) <= 180:
                raise serializers.ValidationError(
                    {"longitude": "Collection longitude must be between -180 and 180."}
                )
            return attrs

        errors = {}
        if latitude is None:
            errors["latitude"] = "Collection latitude is required when marking a collection as Collected."
        if longitude is None:
            errors["longitude"] = "Collection longitude is required when marking a collection as Collected."
        if errors:
            raise serializers.ValidationError(errors)

        if not -90 <= float(latitude) <= 90:
            raise serializers.ValidationError(
                {"latitude": "Collection latitude must be between -90 and 90."}
            )
        if not -180 <= float(longitude) <= 180:
            raise serializers.ValidationError(
                {"longitude": "Collection longitude must be between -180 and 180."}
            )

        try:
            customer_latitude = float(customer.latitude)
            customer_longitude = float(customer.longitude)
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                {"customer": "The selected customer does not have valid latitude and longitude."}
            )

        distance = self._distance_in_meters(
            customer_latitude, customer_longitude, latitude, longitude
        )
        if distance > self.MAX_COLLECTION_DISTANCE_METERS:
            raise serializers.ValidationError(
                {
                    "latitude": (
                        f"Collection location is {distance:.1f} m from the customer. "
                        f"It must be within {self.MAX_COLLECTION_DISTANCE_METERS} m."
                    )
                }
            )
        return attrs
