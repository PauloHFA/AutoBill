"""Agendador persistente de alertas, sonecas, resumos e manutenção (APScheduler)."""

from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from autobill.alerts.formatter import (
    format_monthly_summary,
    format_proactive_alert,
    format_weekly_summary,
    get_alert_action_keyboard,
)
from autobill.alerts.policy import (
    decide_bill_alert,
    local_today,
    parse_lead_days,
    should_alert_missing_recurring,
)
from autobill.config import settings
from autobill.db.models import AlertType, Bill, BillStatus
from autobill.db.repository import (
    AlertRepository,
    BillRepository,
    RecurringRepository,
    SettingsRepository,
    SnoozeRepository,
    db,
)
from autobill.logging_config import get_logger
from autobill.ops.backup import create_backup
from autobill.ops.health import record_error, runtime_status
from autobill.ops.retention import cleanup_expired_files, purge_expired_soft_deletes

logger = get_logger(__name__)


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    return (7, 30)


_current_scheduler: "AlertScheduler | None" = None


async def _job_daily_alerts() -> None:
    sched = _current_scheduler or AlertScheduler()
    await sched.run_daily_alert_cycle()


async def _job_periodic_sweeps() -> None:
    sched = _current_scheduler or AlertScheduler()
    await sched.run_periodic_sweeps()


async def _job_weekly_summary() -> None:
    sched = _current_scheduler or AlertScheduler()
    await sched.run_weekly_summary()


async def _job_monthly_summary() -> None:
    sched = _current_scheduler or AlertScheduler()
    await sched.run_monthly_summary()


async def _job_daily_maintenance() -> None:
    sched = _current_scheduler or AlertScheduler()
    await sched.run_daily_maintenance()


class AlertScheduler:
    """Orquestrador de jobs agendados com persistência SQLite e misfire grace."""

    def __init__(self, bot: Any | None = None, *, use_jobstore: bool = True):
        self.bot = bot
        jobstores = (
            {"default": SQLAlchemyJobStore(url=f"sqlite:///{settings.db_path}")}
            if use_jobstore
            else {}
        )
        self.scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            timezone=settings.default_timezone,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 3600 * 12,  # Recupera alertas perdidos após reinício (RF18)
            },
        )
        self._started = False

    def start(self) -> None:
        global _current_scheduler
        if self._started:
            return
        _current_scheduler = self
        hour, minute = _parse_hhmm(settings.alert_time)
        self.scheduler.add_job(
            _job_daily_alerts,
            CronTrigger(hour=hour, minute=minute, timezone=settings.default_timezone),
            id="daily_alerts",
            replace_existing=True,
        )
        self.scheduler.add_job(
            _job_periodic_sweeps,
            IntervalTrigger(minutes=2),
            id="periodic_sweeps",
            replace_existing=True,
        )
        self.scheduler.add_job(
            _job_weekly_summary,
            CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=settings.default_timezone),
            id="weekly_summary",
            replace_existing=True,
        )
        self.scheduler.add_job(
            _job_monthly_summary,
            CronTrigger(day=1, hour=8, minute=0, timezone=settings.default_timezone),
            id="monthly_summary",
            replace_existing=True,
        )
        self.scheduler.add_job(
            _job_daily_maintenance,
            CronTrigger(hour=3, minute=0, timezone=settings.default_timezone),
            id="daily_maintenance",
            replace_existing=True,
        )
        self.scheduler.start()
        self._started = True
        runtime_status["scheduler_running"] = True
        self._refresh_next_alert_eta()
        logger.info("alert_scheduler_started", alert_time=settings.alert_time)

    def stop(self) -> None:
        global _current_scheduler
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False
            runtime_status["scheduler_running"] = False
            if _current_scheduler is self:
                _current_scheduler = None

    def reschedule_daily_alerts(self, alert_time: str, tz_name: str | None = None) -> None:
        hour, minute = _parse_hhmm(alert_time)
        tz = tz_name or settings.default_timezone
        self.scheduler.add_job(
            _job_daily_alerts,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            id="daily_alerts",
            replace_existing=True,
        )
        self._refresh_next_alert_eta()

    def _refresh_next_alert_eta(self) -> None:
        job = self.scheduler.get_job("daily_alerts")
        if job and job.next_run_time:
            runtime_status["next_alert_at"] = job.next_run_time.isoformat()

    async def _send_telegram_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Any | None = None,
    ) -> int | None:
        if self.bot is None:
            return None
        try:
            msg = await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return int(msg.message_id)
        except Exception as exc:
            record_error(f"Telegram send error ({chat_id}): {exc}")
            logger.exception("telegram_send_failed", chat_id=chat_id)
            return None

    async def run_daily_alert_cycle(self) -> int:
        """
        Executa verificação e envio de alertas D-n, D0 e ATRASO com idempotência por (bill, rule, date).
        """
        sent_count = 0
        now_utc = datetime.now(UTC)

        async with db.session() as session:
            bill_repo = BillRepository(session)
            alert_repo = AlertRepository(session)
            settings_repo = SettingsRepository(session)

            # Atualiza status PENDENTE -> VENCIDA no fuso padrão antes de avaliar alertas
            today_default = local_today(settings.default_timezone)
            await bill_repo.mark_overdue_bills(today_default)

            bills = await bill_repo.get_pending_bills()
            for bill in bills:
                if bill.vencimento is None or bill.user_id is None:
                    continue
                if bill.snoozed_until is not None:
                    snooze_dt = (
                        bill.snoozed_until.replace(tzinfo=UTC)
                        if bill.snoozed_until.tzinfo is None
                        else bill.snoozed_until
                    )
                    if snooze_dt > now_utc:
                        continue

                user_cfg = await settings_repo.get_or_create(bill.user_id)
                if user_cfg.muted:
                    continue

                today_user = local_today(user_cfg.timezone)
                lead_days = parse_lead_days(user_cfg.lead_days)
                last_overdue = await alert_repo.get_last_overdue_sent_date(bill.id)

                decision = decide_bill_alert(
                    bill_due=bill.vencimento.date(),
                    today=today_user,
                    lead_days=lead_days,
                    last_overdue_sent=last_overdue,
                )
                if decision is None:
                    continue

                already = await alert_repo.was_sent(
                    bill.id,
                    decision.alert_type,
                    rule=decision.rule,
                    sent_date=today_user,
                )
                if already:
                    continue

                text = format_proactive_alert(bill, decision)
                kb = get_alert_action_keyboard(bill)
                msg_id = await self._send_telegram_message(bill.user_id, text, reply_markup=kb)
                await alert_repo.mark_sent(
                    bill.id,
                    decision.alert_type,
                    rule=decision.rule,
                    sent_date=today_user,
                    telegram_message_id=msg_id,
                )
                sent_count += 1

            # Checa faturas recorrentes ausentes (RF16)
            rec_repo = RecurringRepository(session)
            for rec in await rec_repo.get_active():
                if rec.user_id is None or rec.last_seen is None:
                    continue
                should_alert, predicted = should_alert_missing_recurring(
                    last_seen=rec.last_seen,
                    typical_day=rec.dia_vencimento,
                    today=today_default,
                    last_alerted_at=rec.last_alerted_at,
                )
                if should_alert:
                    msg = (
                        f"🔔 <b>Aviso de recorrência</b>\n\n"
                        f"A fatura de <b>{escape(rec.beneficiario)}</b> ainda não chegou "
                        f"(vencimento previsto: {predicted.strftime('%d/%m/%Y')})."
                    )
                    await self._send_telegram_message(rec.user_id, msg)
                    rec.last_alerted_at = today_default

        self._refresh_next_alert_eta()
        if sent_count:
            logger.info("daily_alerts_dispatched", count=sent_count)
        return sent_count

    async def run_periodic_sweeps(self) -> None:
        """Dispara sonecas vencidas, atualiza atrasos e lembra revisões pendentes > 24h."""
        now_utc = datetime.now(UTC)
        today_default = local_today(settings.default_timezone)

        async with db.session() as session:
            bill_repo = BillRepository(session)
            snooze_repo = SnoozeRepository(session)

            await bill_repo.mark_overdue_bills(today_default)

            # 1. Sonecas vencidas (1h, 3h, amanhã, N dias)
            due_snoozes = await snooze_repo.get_due_snoozes(now_utc)
            for sn in due_snoozes:
                bill = await bill_repo.get_by_id(sn.bill_id)
                if bill and bill.status in (BillStatus.PENDENTE, BillStatus.VENCIDA):
                    days_until = (
                        (bill.vencimento.date() - today_default).days if bill.vencimento else 0
                    )
                    from autobill.alerts.policy import AlertDecision

                    decision = AlertDecision(
                        alert_type=AlertType.ATRASO if days_until < 0 else AlertType.D0,
                        rule="SNOOZE",
                        days_until=days_until,
                    )
                    text = (
                        f"⏰ <b>Lembrete reagendado</b>\n\n{format_proactive_alert(bill, decision)}"
                    )
                    await self._send_telegram_message(
                        sn.chat_id, text, reply_markup=get_alert_action_keyboard(bill)
                    )
                    bill.snoozed_until = None
                await snooze_repo.mark_done(sn.id)

            # 2. Lembrete de revisão pendente há mais de 24h (RF09)
            cutoff = now_utc - timedelta(hours=24)
            pending_reviews = await bill_repo.get_pending_review_for_reminder(cutoff)
            for bill in pending_reviews:
                if bill.user_id:
                    nome = escape(bill.beneficiario or f"Boleto #{bill.id}")
                    await self._send_telegram_message(
                        bill.user_id,
                        f"✏️ <b>Lembrete de revisão pendente</b>\n\n"
                        f"O boleto #{bill.id} (<b>{nome}</b>) aguarda confirmação há mais de 24h.\n"
                        f"Use /review para confirmar ou descartar.",
                    )
                    bill.review_reminded_at = now_utc

        from autobill.ingest.queue import process_retry_queue

        await process_retry_queue(self.bot)

    async def run_weekly_summary(self) -> None:
        """Envia resumo semanal aos domingos 18:00 para chats autorizados (RF20)."""
        today = local_today(settings.default_timezone)
        week_ahead = today + timedelta(days=7)
        week_ago = datetime.now(UTC) - timedelta(days=7)

        async with db.session() as session:
            for chat_id in settings.allowed_chat_ids:
                res = await session.execute(
                    select(Bill).where(Bill.user_id == chat_id, Bill.deleted_at.is_(None))
                )
                bills = list(res.scalars().all())
                upcoming = [
                    b
                    for b in bills
                    if b.status == BillStatus.PENDENTE
                    and b.vencimento is not None
                    and today <= b.vencimento.date() <= week_ahead
                ]
                overdue = [b for b in bills if b.status == BillStatus.VENCIDA]
                paid_recent = [
                    b
                    for b in bills
                    if b.status == BillStatus.PAGO
                    and b.paid_at is not None
                    and (b.paid_at.replace(tzinfo=UTC) if b.paid_at.tzinfo is None else b.paid_at)
                    >= week_ago
                ]
                text = format_weekly_summary(upcoming, overdue, paid_recent)
                await self._send_telegram_message(chat_id, text)

    async def run_monthly_summary(self) -> None:
        """Envia fechamento do mês anterior no dia 1º de cada mês (RF20)."""
        today = local_today(settings.default_timezone)
        first_this_month = today.replace(day=1)
        prev_month_last = first_this_month - timedelta(days=1)
        year, month = prev_month_last.year, prev_month_last.month

        async with db.session() as session:
            repo = BillRepository(session)
            for chat_id in settings.allowed_chat_ids:
                summary = await repo.get_month_summary(year, month, user_id=chat_id)
                text = format_monthly_summary(year, month, summary)
                await self._send_telegram_message(chat_id, text)

    async def run_daily_maintenance(self) -> None:
        """Executa backup diário e políticas de retenção (RF23, RF24, RF15)."""
        try:
            if settings.backup_enabled:
                create_backup()
            await cleanup_expired_files()
            await purge_expired_soft_deletes(days=30)
        except Exception as exc:
            record_error(f"Maintenance error: {exc}")
            logger.exception("daily_maintenance_failed")
