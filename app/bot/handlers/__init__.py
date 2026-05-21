from aiogram import Router

from app.bot.handlers.calendar import router as calendar_router
from app.bot.handlers.households import router as households_router
from app.bot.handlers.messages import router as messages_router
from app.bot.handlers.receipts import router as receipts_router
from app.bot.handlers.start import router as start_router
from app.bot.handlers.tasks import router as tasks_router

router = Router()
router.include_router(start_router)
router.include_router(households_router)
router.include_router(calendar_router)
router.include_router(receipts_router)
router.include_router(tasks_router)
router.include_router(messages_router)
