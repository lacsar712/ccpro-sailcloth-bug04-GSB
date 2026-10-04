from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from .models import ClothRoll, DipRun, Loft
from .rules import can_mark_roll_cured


def make_roll():
    loft = Loft.objects.create(name="一号间")
    return ClothRoll.objects.create(
        loft=loft, roll_code="R-1", status=ClothRoll.STATUS_DIPPING
    )


def add_dip(roll, *, started_at, resin_pct, cure_hours):
    return DipRun.objects.create(
        roll=roll,
        started_at=started_at,
        resin_pct=Decimal(str(resin_pct)),
        cure_hours=None if cure_hours is None else Decimal(str(cure_hours)),
    )


class CanMarkCuredRuleTests(APITestCase):
    def test_no_dip_run_blocked(self):
        roll = make_roll()
        ok, msg = can_mark_roll_cured(roll)
        self.assertFalse(ok)
        self.assertIn("尚无浸渍记录", msg)

    def test_empty_cure_hours_blocked_even_when_resin_looks_enough(self):
        # 树脂 28% 远高于 12，但固化时长为空：必须拒绝
        roll = make_roll()
        add_dip(roll, started_at=timezone.now(), resin_pct=28, cure_hours=None)
        ok, msg = can_mark_roll_cured(roll)
        self.assertFalse(ok)
        self.assertIn("尚未填写固化时长", msg)

    def test_resin_above_twelve_does_not_substitute_for_cure_hours(self):
        # 树脂 99%、时长为空，仍不能放行
        roll = make_roll()
        add_dip(roll, started_at=timezone.now(), resin_pct=99, cure_hours=None)
        ok, _ = can_mark_roll_cured(roll)
        self.assertFalse(ok)

    def test_cure_below_twelve_blocked_even_when_resin_high(self):
        roll = make_roll()
        add_dip(roll, started_at=timezone.now(), resin_pct=28, cure_hours=10)
        ok, msg = can_mark_roll_cured(roll)
        self.assertFalse(ok)
        self.assertIn("低于", msg)

    def test_cure_exactly_twelve_passes_even_if_resin_below_twelve(self):
        # 时长刚好 12h；树脂仅 8%，只作对照，不影响放行
        roll = make_roll()
        add_dip(roll, started_at=timezone.now(), resin_pct=8, cure_hours=12)
        ok, msg = can_mark_roll_cured(roll)
        self.assertTrue(ok, msg)

    def test_cure_above_twelve_passes(self):
        roll = make_roll()
        add_dip(roll, started_at=timezone.now(), resin_pct=28.5, cure_hours=14.5)
        ok, _ = can_mark_roll_cured(roll)
        self.assertTrue(ok)

    def test_only_latest_dip_run_counts(self):
        # 旧浸渍 14h 达标，但最近一条时长为空：必须拒绝
        roll = make_roll()
        now = timezone.now()
        add_dip(roll, started_at=now - timedelta(hours=20), resin_pct=28, cure_hours=14)
        add_dip(roll, started_at=now, resin_pct=28, cure_hours=None)
        ok, _ = can_mark_roll_cured(roll)
        self.assertFalse(ok)

    def test_patch_to_cured_enforces_rule_over_api(self):
        # 两名管理员交叉点标：时长真够的那笔成功，只有树脂够的那笔失败
        from django.contrib.auth import get_user_model

        admin_a = get_user_model().objects.create_user(
            "adminA", password="x12345!", is_staff=True
        )
        admin_b = get_user_model().objects.create_user(
            "adminB", password="x12345!", is_staff=True
        )

        good = make_roll()
        add_dip(good, started_at=timezone.now(), resin_pct=8, cure_hours=12)

        bad = make_roll()
        bad.roll_code = "R-2"
        bad.save()
        add_dip(bad, started_at=timezone.now(), resin_pct=99, cure_hours=None)

        self.client.force_authenticate(admin_a)
        resp_a = self.client.patch(f"/api/rolls/{good.id}/", {"status": "cured"}, format="json")
        self.assertEqual(resp_a.status_code, 200, resp_a.content)
        good.refresh_from_db()
        self.assertEqual(good.status, ClothRoll.STATUS_CURED)

        self.client.force_authenticate(admin_b)
        resp_b = self.client.patch(f"/api/rolls/{bad.id}/", {"status": "cured"}, format="json")
        self.assertEqual(resp_b.status_code, 400, resp_b.content)
        bad.refresh_from_db()
        self.assertNotEqual(bad.status, ClothRoll.STATUS_CURED)
