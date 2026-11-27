import nonebot

nonebot.require("nonebot_plugin_orm")
nonebot.require("nonebot_plugin_apscheduler")

from . import (
    binding as binding,
    notifications as notifications,
    pr_review as pr_review,
    project_assignment as project_assignment,
    review_reminder as review_reminder,
)
