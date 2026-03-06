"""Light platform for Govee integration.

Provides light entities with support for:
- On/Off control
- Brightness control
- RGB color
- Color temperature
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (  # type: ignore[attr-defined]
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_ENABLE_DIY_SCENES,
    CONF_ENABLE_SCENES,
    DEFAULT_ENABLE_DIY_SCENES,
    DEFAULT_ENABLE_SCENES,
    SEGMENT_MODE_GROUPED,
    SEGMENT_MODE_INDIVIDUAL,
)
from .coordinator import GoveeCoordinator
from .entity import GoveeEntity
from .models import (
    BrightnessCommand,
    ColorCommand,
    ColorTempCommand,
    GoveeDevice,
    PowerCommand,
    RGBColor,
    SceneCommand,
)
from .platforms.grouped_segment import GoveeGroupedSegmentEntity
from .platforms.segment import GoveeSegmentEntity

_LOGGER = logging.getLogger(__name__)

# Home Assistant brightness range
HA_BRIGHTNESS_MAX = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Govee lights from a config entry."""
    coordinator: GoveeCoordinator = entry.runtime_data

    entities: list[LightEntity] = []

    # Get per-device segment modes
    device_modes = entry.options.get("segment_mode_by_device", {})

    # Check if scenes are enabled in options
    enable_scenes = entry.options.get(CONF_ENABLE_SCENES, DEFAULT_ENABLE_SCENES)
    enable_diy_scenes = entry.options.get(CONF_ENABLE_DIY_SCENES, DEFAULT_ENABLE_DIY_SCENES)

    for device in coordinator.devices.values():
        # Only create light entities for devices with power control (not fans)
        if device.supports_power and not device.is_fan:
            entities.append(
                GoveeLightEntity(coordinator, device, enable_scenes, enable_diy_scenes)
            )

        # Create segment entities for RGBIC devices based on per-device mode
        if device.supports_segments and device.segment_count > 0:
            # Use per-device mode if set, otherwise default to individual
            segment_mode = device_modes.get(device.device_id, SEGMENT_MODE_INDIVIDUAL)

            _LOGGER.debug(
                "Segment check for %s: device_mode=%s, supports_segments=%s, segment_count=%d",
                device.name,
                device_modes.get(device.device_id, "default (individual)"),
                device.supports_segments,
                device.segment_count,
            )

            if segment_mode == SEGMENT_MODE_GROUPED:
                _LOGGER.debug(
                    "Creating grouped segment entity for %s",
                    device.name,
                )
                entities.append(
                    GoveeGroupedSegmentEntity(
                        coordinator=coordinator,
                        device=device,
                    )
                )
            elif segment_mode == SEGMENT_MODE_INDIVIDUAL:
                _LOGGER.debug(
                    "Creating %d individual segment entities for %s",
                    device.segment_count,
                    device.name,
                )
                for segment_index in range(device.segment_count):
                    entities.append(
                        GoveeSegmentEntity(
                            coordinator=coordinator,
                            device=device,
                            segment_index=segment_index,
                        )
                    )

    async_add_entities(entities)
    _LOGGER.debug("Set up %d Govee light entities", len(entities))


class GoveeLightEntity(GoveeEntity, LightEntity, RestoreEntity):
    """Govee light entity.

    Supports:
    - On/Off
    - Brightness (scaled to device range)
    - RGB color
    - Color temperature
    - State restoration for group devices
    """

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        device: GoveeDevice,
        enable_scenes: bool = True,
        enable_diy_scenes: bool = True,
    ) -> None:
        """Initialize the light entity."""
        super().__init__(coordinator, device)

        # Set name (uses has_entity_name = True)
        self._attr_name = None  # Use device name

        # Determine supported color modes
        self._attr_supported_color_modes = self._determine_color_modes()
        # Get device brightness range
        self._brightness_min, self._brightness_max = device.brightness_range

        # Effect support: if device has scenes or DIY scenes and they're enabled
        self._enable_scenes = device.supports_scenes and enable_scenes
        self._enable_diy_scenes = device.supports_diy_scenes and enable_diy_scenes
        if self._enable_scenes or self._enable_diy_scenes:
            self._attr_supported_features = LightEntityFeature.EFFECT

        # Scene-to-effect mappings (populated in async_added_to_hass)
        self._effect_to_scene: dict[str, tuple[int, str]] = {}
        self._scene_id_to_effect: dict[str, str] = {}
        self._diy_effect_ids: set[str] = set()  # Effect names that are DIY scenes
        self._effect_names: list[str] = []

    def _determine_color_modes(self) -> set[ColorMode]:
        """Determine supported color modes from device capabilities."""
        modes: set[ColorMode] = set()

        if self._device.supports_rgb:
            modes.add(ColorMode.RGB)

        if self._device.supports_color_temp:
            modes.add(ColorMode.COLOR_TEMP)

        if not modes and self._device.supports_brightness:
            modes.add(ColorMode.BRIGHTNESS)

        if not modes:
            modes.add(ColorMode.ONOFF)

        return modes

    @property
    def color_mode(self) -> ColorMode:
        """Return current color mode based on device state.

        Dynamically computed so it always reflects actual state.
        Always returns a value from supported_color_modes to satisfy
        HA Core validation (color_mode must be in supported_color_modes).
        """
        state = self.device_state
        modes = self.supported_color_modes or {ColorMode.ONOFF}

        if state and state.color_temp_kelvin is not None:
            if ColorMode.COLOR_TEMP in modes:
                return ColorMode.COLOR_TEMP

        if state and state.color is not None:
            if ColorMode.RGB in modes:
                return ColorMode.RGB

        # Default to first supported mode (prefer COLOR_TEMP > BRIGHTNESS > any)
        if ColorMode.BRIGHTNESS in modes:
            return ColorMode.BRIGHTNESS
        if ColorMode.COLOR_TEMP in modes:
            return ColorMode.COLOR_TEMP
        return ColorMode(next(iter(modes)))

    @property
    def is_on(self) -> bool | None:
        """Return True if light is on."""
        state = self.device_state
        return state.power_state if state else None

    @property
    def brightness(self) -> int | None:
        """Return brightness (0-255)."""
        state = self.device_state
        if state is None:
            return None

        # Convert device brightness to HA scale
        return self._device_to_ha_brightness(state.brightness)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return RGB color as (r, g, b) tuple."""
        state = self.device_state
        if state and state.color:
            return state.color.as_tuple
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return color temperature in Kelvin."""
        state = self.device_state
        return state.color_temp_kelvin if state and state.color_temp_kelvin else None

    @property
    def min_color_temp_kelvin(self) -> int:
        """Return minimum color temperature in Kelvin."""
        temp_range = self._device.color_temp_range
        return temp_range.min_kelvin if temp_range else 2000

    @property
    def max_color_temp_kelvin(self) -> int:
        """Return maximum color temperature in Kelvin."""
        temp_range = self._device.color_temp_range
        return temp_range.max_kelvin if temp_range else 9000

    @property
    def effect_list(self) -> list[str] | None:
        """Return list of available effects (scene names)."""
        return self._effect_names if self._effect_names else None

    @property
    def effect(self) -> str | None:
        """Return currently active effect (scene or DIY scene name)."""
        state = self.device_state
        if not state:
            return None

        # Check DIY scene first (DIY scenes are listed first in effects)
        if state.active_diy_scene:
            effect_name = self._scene_id_to_effect.get(state.active_diy_scene)
            if effect_name:
                return effect_name

        # Check regular scene
        if state.active_scene:
            effect_name = self._scene_id_to_effect.get(state.active_scene)
            if effect_name:
                return effect_name
            return state.active_scene_name

        return None

    def _ha_to_device_brightness(self, ha_brightness: int) -> int:
        """Convert HA brightness (0-255) to device range, respecting min."""
        ratio = ha_brightness / HA_BRIGHTNESS_MAX
        return int(
            self._brightness_min + ratio * (self._brightness_max - self._brightness_min)
        )

    def _device_to_ha_brightness(self, device_brightness: int) -> int:
        """Convert device brightness to HA range (0-255), respecting min."""
        device_range = self._brightness_max - self._brightness_min
        if device_range <= 0:
            return 0
        return int(
            (device_brightness - self._brightness_min)
            / device_range
            * HA_BRIGHTNESS_MAX
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on with optional parameters."""
        # Handle effect (scene activation)
        if ATTR_EFFECT in kwargs:
            effect_name = kwargs[ATTR_EFFECT]
            scene_info = self._effect_to_scene.get(effect_name)
            if scene_info:
                scene_id, scene_name = scene_info
                if effect_name in self._diy_effect_ids:
                    await self.coordinator.async_send_diy_scene(
                        self._device_id,
                        scene_id=scene_id,
                        scene_name=scene_name,
                    )
                else:
                    await self.coordinator.async_control_device(
                        self._device_id,
                        SceneCommand(scene_id=scene_id, scene_name=scene_name),
                    )
            else:
                _LOGGER.warning(
                    "Unknown effect '%s' for %s", effect_name, self._device.name
                )
            return

        # Handle brightness
        if ATTR_BRIGHTNESS in kwargs:
            ha_brightness = kwargs[ATTR_BRIGHTNESS]
            device_brightness = self._ha_to_device_brightness(ha_brightness)
            if not await self.coordinator.async_control_device(
                self._device_id,
                BrightnessCommand(brightness=device_brightness),
            ):
                _LOGGER.warning("Brightness command failed for %s", self._device_id)

        # Handle RGB color
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            color = RGBColor(r=r, g=g, b=b)
            if not await self.coordinator.async_control_device(
                self._device_id,
                ColorCommand(color=color),
            ):
                _LOGGER.warning("Color command failed for %s", self._device_id)

        # Handle color temperature
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            if not await self.coordinator.async_control_device(
                self._device_id,
                ColorTempCommand(kelvin=kelvin),
            ):
                _LOGGER.warning("Color temp command failed for %s", self._device_id)

        # Only send power command if light is off or no attributes were set
        has_attribute = any(
            k in kwargs
            for k in (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN)
        )
        if not has_attribute or not self.is_on:
            await self.coordinator.async_control_device(
                self._device_id,
                PowerCommand(power_on=True),
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.async_control_device(
            self._device_id,
            PowerCommand(power_on=False),
        )

    def _build_effect_mapping(
        self,
        scenes: list[dict[str, Any]],
        diy_scenes: list[dict[str, Any]],
    ) -> None:
        """Build effect name mappings from scene and DIY scene data.

        DIY scenes are listed first (user-created, likely preferred),
        followed by regular scenes.
        """
        self._effect_to_scene = {}
        self._scene_id_to_effect = {}
        self._diy_effect_ids = set()
        diy_names: list[str] = []
        scene_names: list[str] = []

        # DIY scenes first — value is a plain int
        for scene_data in diy_scenes:
            scene_id = scene_data.get("value", 0)
            scene_name = scene_data.get("name", f"DIY {scene_id}")

            unique_name = scene_name
            counter = 1
            while unique_name in self._effect_to_scene:
                unique_name = f"{scene_name} ({counter})"
                counter += 1

            self._effect_to_scene[unique_name] = (scene_id, scene_name)
            self._scene_id_to_effect[str(scene_id)] = unique_name
            self._diy_effect_ids.add(unique_name)
            diy_names.append(unique_name)

        # Regular scenes — value is {"id": ..., "paramId": ...}
        for scene_data in scenes:
            scene_id = scene_data.get("value", {}).get("id", 0)
            scene_name = scene_data.get("name", f"Scene {scene_id}")

            unique_name = scene_name
            counter = 1
            while unique_name in self._effect_to_scene:
                unique_name = f"{scene_name} ({counter})"
                counter += 1

            self._effect_to_scene[unique_name] = (scene_id, scene_name)
            self._scene_id_to_effect[str(scene_id)] = unique_name
            scene_names.append(unique_name)

        # DIY first, then regular scenes
        self._effect_names = diy_names + scene_names

    async def async_added_to_hass(self) -> None:
        """Restore state for group devices and load scenes for effects."""
        await super().async_added_to_hass()

        if self._device.is_group:
            last_state = await self.async_get_last_state()
            if last_state:
                # Restore state via coordinator
                power = last_state.state == "on"
                brightness = None
                if last_state.attributes.get("brightness"):
                    brightness = self._ha_to_device_brightness(
                        last_state.attributes["brightness"]
                    )
                self.coordinator.restore_group_state(self._device_id, power, brightness)

        # Load scenes for effect support (skip group devices - no scene API support)
        if (self._enable_scenes or self._enable_diy_scenes) and not self._device.is_group:
            scenes: list[dict[str, Any]] = []
            diy_scenes: list[dict[str, Any]] = []

            if self._enable_scenes:
                scenes = await self.coordinator.async_get_scenes(self._device_id)
            if self._enable_diy_scenes:
                diy_scenes = await self.coordinator.async_get_diy_scenes(self._device_id)

            if scenes or diy_scenes:
                self._build_effect_mapping(scenes, diy_scenes)
