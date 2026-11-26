"""Combined-trip completion and the one-trip-at-a-time rule for a vehicle/crew."""
from datetime import time
from decimal import Decimal

import pytest

from app.models.core_modules.daily_operations.daily_trip_assignment import DailyTripAssignment
from app.models.core_modules.daily_operations.daily_trip_collection_point import (
    DailyTripCollectionPoint,
)
from app.models.core_modules.daily_operations.daily_trip_household_collection import (
    DailyTripHouseholdCollection,
)
from tests.schedule_setup.test_daily_ops_parity import *  # noqa: F401,F403 (fixtures)

# Keep only the fixtures — don't re-collect that module's test classes here.
for _name in [n for n in list(globals()) if n.startswith("Test")]:
    del globals()[_name]


def _collected_bin_stop(assignment, collection_point, bin_obj):
    return DailyTripCollectionPoint.objects.create(
        trip_assignment_id=assignment.unique_id,
        collection_point_id=collection_point.unique_id,
        bin_id=bin_obj.unique_id,
        collected_weight_kg=Decimal("10.00"),
        is_collected=True,
        status=DailyTripCollectionPoint.STATUS_COLLECTED,
    )


def _household_stop(assignment, status=DailyTripHouseholdCollection.STATUS_PENDING):
    return DailyTripHouseholdCollection.objects.create(
        trip_assignment_id=assignment.unique_id,
        customer_id="CUST-COMBINED-1",
        status=status,
        is_collected=status == DailyTripHouseholdCollection.STATUS_COLLECTED,
        company_id=assignment.company_id,
        project_id=assignment.project_id,
    )


@pytest.mark.django_db
class TestCombinedTripCompletion:
    def test_bins_done_but_households_pending_does_not_complete(
        self, assignment, collection_point, bin_obj
    ):
        _collected_bin_stop(assignment, collection_point, bin_obj)
        _household_stop(assignment)

        assert assignment.mark_completed_if_all_cps_collected() is False
        assignment.refresh_from_db()
        assert assignment.status != DailyTripAssignment.STATUS_COMPLETED

    def test_households_done_but_bins_pending_does_not_complete(
        self, assignment, collection_point, bin_obj
    ):
        DailyTripCollectionPoint.objects.create(
            trip_assignment_id=assignment.unique_id,
            collection_point_id=collection_point.unique_id,
            bin_id=bin_obj.unique_id,
        )
        _household_stop(assignment, DailyTripHouseholdCollection.STATUS_COLLECTED)

        assert assignment.mark_completed_if_all_household_stops_collected() is False
        assignment.refresh_from_db()
        assert assignment.status != DailyTripAssignment.STATUS_COMPLETED

    def test_everything_done_completes(self, assignment, collection_point, bin_obj):
        _collected_bin_stop(assignment, collection_point, bin_obj)
        _household_stop(assignment, DailyTripHouseholdCollection.STATUS_COLLECTED)

        assert assignment.mark_completed_if_all_cps_collected() is True
        assignment.refresh_from_db()
        assert assignment.status == DailyTripAssignment.STATUS_COMPLETED


@pytest.mark.django_db
class TestOneTripAtATime:
    def _second_trip(self, first, scheduled):
        return DailyTripAssignment.objects.create(
            company_id=first.company_id,
            project_id=first.project_id,
            trip_plan_id=first.trip_plan_id,
            trip_date=first.trip_date,
            vehicle_id=first.vehicle_id,
            staff_template_id=first.staff_template_id,
            scheduled_time=scheduled,
        )

    def test_running_trip_blocks_an_earlier_scheduled_one(self, assignment):
        assignment.scheduled_time = time(9, 0)
        assignment.save(update_fields=["scheduled_time"])
        assignment.mark_started()
        earlier = self._second_trip(assignment, time(6, 30))

        assert earlier.start_blocking_assignment() == assignment
        assert earlier.collection_blocking_assignment() == assignment

    def test_running_trip_itself_is_not_blocked_for_collection(self, assignment):
        assignment.mark_started()
        self._second_trip(assignment, time(6, 30))

        assert assignment.collection_blocking_assignment() is None

    def test_finished_trip_does_not_block(self, assignment):
        assignment.mark_started()
        assignment.mark_ended()
        later = self._second_trip(assignment, time(23, 0))

        assert later.start_blocking_assignment() is None
