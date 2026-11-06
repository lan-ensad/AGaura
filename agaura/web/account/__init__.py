"""Accounts area (SPEC §5.1, §12.4): signup, invitations, demo, verification, password reset,
account settings."""

from fastapi import APIRouter

from agaura.web.account import access, demo, invitation, profile

router = APIRouter()
router.include_router(access.router)
router.include_router(invitation.router)
router.include_router(demo.router)
router.include_router(profile.router)
