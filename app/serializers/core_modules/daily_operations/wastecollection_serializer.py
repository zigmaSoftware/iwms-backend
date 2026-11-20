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

    class Meta:
        model = WasteCollection
        fields = "__all__"
        read_only_fields = ["unique_id", "total_quantity", "collection_time"]
        extra_kwargs = {
            "customer": {"write_only": True},
        }
        list_serializer_class = WasteCollectionListSerializer

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

        customer = CustomerCreation.objects.filter(
            unique_id=customer_id, is_deleted=False
        ).first()
        if not customer:
            raise serializers.ValidationError({"customer": "A valid customer is required."})
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
