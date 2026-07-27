"""Shared typing helpers for the Xiaomi M365 Scooter integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from .coordinator import M365DataCoordinator

type M365ConfigEntry = ConfigEntry[M365DataCoordinator]
