import nonebot

nonebot.require("nonebot_plugin_orm")
nonebot.require("nonebot_plugin_apscheduler")

from src.service.github.models import ReviewConfig

from . import (
    binding as binding,
    notifications as notifications,
    project_assignment as project_assignment,
    review_reminder as review_reminder,
)

if nonebot.get_plugin_config(ReviewConfig).review_enabled:
    from . import pr_review as pr_review
