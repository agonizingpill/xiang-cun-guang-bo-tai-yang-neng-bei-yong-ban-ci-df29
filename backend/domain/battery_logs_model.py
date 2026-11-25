from sqlalchemy import (Boolean, CheckConstraint, Column, Float, ForeignKey,
                        Integer, JSON, String, Table, DateTime)

from core.db import metadata


battery_logs = Table('battery_logs', metadata,
    Column('code', String(64), primary_key=True),
    Column('meter', String(160), nullable=False),
    Column('hour', Integer, nullable=False),
    Column('load_wh', Float, nullable=False),
    Column('clear', Boolean, nullable=False),
    Column('verified', Boolean, nullable=False),
    # --- 抄数判定（复核预测 / 误差带 / 挂起与换档段） ---
    Column('verdict', String(32), nullable=True),
    Column('verdict_label', String(120), nullable=True),
    Column('predicted_wh', Float, nullable=True),
    Column('band_wh', Float, nullable=True),
    Column('deviation_wh', Float, nullable=True),
    Column('direction', String(8), nullable=True),
    Column('segment_code', String(64), nullable=True),
    Column('range_token', String(80), nullable=True),
    Column('revision', Integer, nullable=True),
    Column('judged_by', String(100), nullable=True),
    Column('judged_at', String(40), nullable=True),
    CheckConstraint('hour between 0 and 47'),
    CheckConstraint('load_wh >= 0'),
)


# 待审 / 已确认的换档段：连续至少三小时同向超出历史误差带。
review_segments = Table('review_segments', metadata,
    Column('code', String(64), primary_key=True),
    Column('meter', String(160), nullable=False, index=True),
    Column('start_hour', Integer, nullable=False),
    Column('end_hour', Integer, nullable=False),
    Column('hours', Integer, nullable=False),
    Column('direction', String(8), nullable=False),
    Column('records', JSON, nullable=False),
    Column('range_token', String(80), nullable=False),
    Column('revision', Integer, nullable=False, default=1),
    # pending=待复核员确认换档；confirmed=已确认整段审签；superseded=范围已被晚到抄录改变
    Column('status', String(16), nullable=False, default='pending'),
    Column('confirmed_by', String(100), nullable=True),
    Column('confirmed_at', DateTime, nullable=True),
    Column('created_at', DateTime, nullable=True),
)


# 每只电表的复核版本号：补录 / 重判都会推进，供确认换档时做乐观并发校验。
meter_review_state = Table('meter_review_state', metadata,
    Column('meter', String(160), primary_key=True),
    Column('revision', Integer, nullable=False, default=1),
    Column('updated_at', DateTime, nullable=True),
)
