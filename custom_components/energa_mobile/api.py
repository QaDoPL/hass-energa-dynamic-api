"""API interface for Energa My Meter."""

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    BASE_URL,
    CHART_ENDPOINT,
    DATA_ENDPOINT,
    HEADERS,
    LOGIN_ENDPOINT,
    SESSION_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)


class EnergaAuthError(Exception):
    pass


class EnergaConnectionError(Exception):
    pass


class EnergaTokenExpiredError(Exception):
    pass


class EnergaAPI:
    def __init__(
        self,
        username,
        password,
        device_token: str,
        session: aiohttp.ClientSession,
        create_session_fn=None,
    ):
        self._username = username
        self._password = password
        self._device_token = device_token  # Unique per-installation token
        self._session = session
        self._create_session_fn = create_session_fn or (lambda: aiohttp.ClientSession())
        self._token = None  # Server-returned token (may be empty in newer API)
        self._meters_data = []
        self._hass = None  # Reference to HA instance for statistics queries
        self._api_warning = None  # Last non-null warning from API
        self._api_error = None  # Last non-null error from API
        self._energa24_refresh_token = None
        self._energa24_access_token = None
        self._energa24_token_expires = 0
        self._energa24_account_id = None
        self._energa24_price_list_id = None
        self._energa24_session = None  # Dedicated session for 24.energa.pl
        self._energa24_token_updated_cb = None  # Callback for token rotation persistence

    def set_hass(self, hass):
        """Set Home Assistant instance reference for database queries."""
        self._hass = hass

    def has_multi_zone_meters(self) -> bool:
        """Check if any meter uses a multi-zone tariff (e.g. G12w)."""
        return any(m.get("zone_count", 1) > 1 for m in self._meters_data)

    async def async_login(self) -> bool:
        try:
            # Clear old cookies/session state before re-login
            self._session.cookie_jar.clear()
            self._token = None
            _LOGGER.debug("Cleared session cookies, attempting fresh login")

            await self._api_get(SESSION_ENDPOINT)
            # Use persistent device token from config (generated during installation)
            params = {
                "clientOS": "ios",
                "notifyService": "APNs",
                "username": self._username,
                "password": self._password,
                "token": self._device_token,
            }
            async with self._session.get(
                f"{BASE_URL}{LOGIN_ENDPOINT}", headers=HEADERS, params=params
            ) as resp:
                if resp.status != 200:
                    raise EnergaConnectionError(f"Login HTTP {resp.status}")
                try:
                    data = await resp.json()
                except (ValueError, TypeError, aiohttp.ContentTypeError):
                    raise EnergaConnectionError("Invalid JSON")
                if not data.get("success"):
                    error_msg = str(data.get("error") or data.get("message") or "")
                    if error_msg and any(
                        kw in error_msg.lower() for kw in (
                            "login", "password", "username", "credentials", "auth",
                        )
                    ):
                        raise EnergaAuthError(
                            f"Invalid credentials (API: {error_msg})"
                        )
                    raise EnergaConnectionError(
                        "API returned success=False (possible server outage)"
                    )

                # Token might be missing in newer API versions; session cookies are sufficient
                self._token = data.get("token") or (data.get("response") or {}).get(
                    "token"
                )
                _LOGGER.info(
                    "Login successful. Token received: %s, Cookies: %d",
                    bool(self._token),
                    len(self._session.cookie_jar),
                )
                return True
        except aiohttp.ClientError as err:
            _LOGGER.error("Login network error: %s", err)
            raise EnergaConnectionError from err

    async def async_get_data(self, force_refresh: bool = False) -> list[dict]:
        if force_refresh:
            self._meters_data = []
        if not self._meters_data:
            self._meters_data = await self._fetch_all_meters()

        tz = ZoneInfo("Europe/Warsaw")
        # Construct midnight using datetime constructor (not .replace())
        # to correctly resolve UTC offset on DST transition days (#26)
        today = datetime.now(tz).date()
        ts = int(
            datetime(today.year, today.month, today.day, 0, 0, 0,
                     tzinfo=tz).timestamp() * 1000
        )

        updated_meters = []
        for meter in self._meters_data:
            m_data = meter.copy()
            if m_data.get("obis_plus"):
                # Fetch total daily consumption (sum of all zones)
                vals = await self._fetch_chart(
                    m_data["meter_point_id"], m_data["obis_plus"], ts
                )
                m_data["daily_pobor"] = sum(vals)

                # Fetch per-zone daily consumption for G12w
                if m_data.get("zone_count", 1) > 1:
                    vals_1 = await self._fetch_chart(
                        m_data["meter_point_id"], m_data["obis_plus"], ts, zone_index=0
                    )
                    vals_2 = await self._fetch_chart(
                        m_data["meter_point_id"], m_data["obis_plus"], ts, zone_index=1
                    )
                    m_data["daily_pobor_1"] = sum(vals_1)
                    m_data["daily_pobor_2"] = sum(vals_2)

            if m_data.get("obis_minus"):
                vals = await self._fetch_chart(
                    m_data["meter_point_id"], m_data["obis_minus"], ts
                )
                m_data["daily_produkcja"] = sum(vals)

            _LOGGER.debug(
                "Energa Meter [%s]: Total(+)=%s, Total(-)=%s, Daily(+)=%s, Daily(-)=%s",
                m_data.get("meter_serial"),
                m_data.get("total_plus"),
                m_data.get("total_minus"),
                m_data.get("daily_pobor"),
                m_data.get("daily_produkcja"),
            )
            updated_meters.append(m_data)
        self._meters_data = updated_meters
        return updated_meters

    async def async_get_history_hourly(
        self, meter_point_id, date: datetime, include_timestamps: bool = False
    ):
        meter = next(
            (m for m in self._meters_data if m["meter_point_id"] == meter_point_id),
            None,
        )
        if not meter:
            await self.async_get_data()
            meter = next(
                (m for m in self._meters_data if m["meter_point_id"] == meter_point_id),
                None,
            )
            if not meter:
                return {"import": [], "export": []}

        # Construct midnight using datetime constructor (not .replace())
        # to correctly resolve UTC offset on DST transition days (#26).
        tz = ZoneInfo("Europe/Warsaw")
        day = date.date() if hasattr(date, 'date') else date
        ts = int(
            datetime(day.year, day.month, day.day, 0, 0, 0,
                     tzinfo=tz).timestamp() * 1000
        )

        result = {"import": [], "export": []}
        if meter.get("obis_plus"):
            # Total import (sum of all zones)
            result["import"] = await self._fetch_chart(
                meter["meter_point_id"], meter["obis_plus"], ts,
                include_timestamps=include_timestamps,
            )
            # Per-zone import for G12w
            if meter.get("zone_count", 1) > 1:
                result["import_1"] = await self._fetch_chart(
                    meter["meter_point_id"], meter["obis_plus"], ts,
                    zone_index=0, include_timestamps=include_timestamps,
                )
                result["import_2"] = await self._fetch_chart(
                    meter["meter_point_id"], meter["obis_plus"], ts,
                    zone_index=1, include_timestamps=include_timestamps,
                )
        if meter.get("obis_minus"):
            result["export"] = await self._fetch_chart(
                meter["meter_point_id"], meter["obis_minus"], ts,
                include_timestamps=include_timestamps,
            )
            # Per-zone export for G12w
            if meter.get("zone_count", 1) > 1:
                result["export_1"] = await self._fetch_chart(
                    meter["meter_point_id"], meter["obis_minus"], ts,
                    zone_index=0, include_timestamps=include_timestamps,
                )
                result["export_2"] = await self._fetch_chart(
                    meter["meter_point_id"], meter["obis_minus"], ts,
                    zone_index=1, include_timestamps=include_timestamps,
                )

        _LOGGER.debug(
            "History %s (ts=%s): Import=%d pts, Export=%d pts",
            date.date(),
            ts,
            len(result["import"]),
            len(result["export"]),
        )

        return result

    async def async_get_hourly_statistics(
        self, meter_point_id: str, start_date: datetime = None
    ):
        """Fetch hourly data from start_date to now (smart fetch).

        Returns:
            dict with keys like "import", "import_1", "import_2", "export"
            containing lists of: {"start": datetime, "state": float}
        """
        from datetime import timedelta

        tz = ZoneInfo("Europe/Warsaw")
        now = datetime.now(tz)

        # Default: 30 days ago if no start_date provided
        if start_date is None:
            start_date = now - timedelta(days=30)

        # Ensure start_date is timezone-aware
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=tz)

        # Get current meter data
        meter = next(
            (m for m in self._meters_data if m["meter_point_id"] == meter_point_id),
            None,
        )
        if not meter:
            _LOGGER.warning("Meter %s not found for statistics", meter_point_id)
            return {"import": [], "export": []}

        has_zones = meter.get("zone_count", 1) > 1

        # Calculate how many days to fetch
        days_to_fetch = (now.date() - start_date.date()).days + 1
        days_to_fetch = max(1, min(days_to_fetch, 365))  # Cap at 365 days

        _LOGGER.debug(
            "Smart fetch for %s: from %s (%d days, zones=%s)",
            meter_point_id,
            start_date.date(),
            days_to_fetch,
            has_zones,
        )

        # Collect hourly data points
        keys = ["import", "export"]
        if has_zones:
            keys.extend(["import_1", "import_2", "export_1", "export_2"])
        all_points = {k: [] for k in keys}

        for day_offset in range(days_to_fetch):
            target_date = start_date + timedelta(days=day_offset)

            # Skip future dates
            if target_date.date() > now.date():
                break

            if day_offset > 0:
                await asyncio.sleep(0.3)

            day_data = await self.async_get_history_hourly(
                meter_point_id, target_date, include_timestamps=True
            )

            # Process each data key — use API-provided timestamps
            # instead of computing from array index (#26).
            # On DST spring-forward, the API returns 23 points (not 24)
            # with correct Unix timestamps for each hour.
            for key in keys:
                for item in day_data.get(key, []):
                    # With include_timestamps=True, items are
                    # (value, timestamp_ms) tuples
                    if isinstance(item, (list, tuple)):
                        hourly_value, tm_ms = item
                    else:
                        # Fallback for unexpected format
                        continue

                    if hourly_value is not None and hourly_value >= 0:
                        hour_dt = datetime.fromtimestamp(
                            tm_ms / 1000, tz=tz
                        )

                        # Only include points after start_date
                        if hour_dt >= start_date:
                            all_points[key].append(
                                {
                                    "start": hour_dt,
                                    "state": hourly_value,
                                }
                            )

        # Sort by time (oldest first)
        for key in keys:
            all_points[key].sort(key=lambda x: x["start"])

        _LOGGER.info(
            "Smart fetch for %s: %d import, %d export points (from %s)%s",
            meter_point_id,
            len(all_points["import"]),
            len(all_points["export"]),
            start_date.date(),
            (
                f", imp_z1={len(all_points.get('import_1', []))}"
                f", imp_z2={len(all_points.get('import_2', []))}"
                f", exp_z1={len(all_points.get('export_1', []))}"
                f", exp_z2={len(all_points.get('export_2', []))}"
            )
            if has_zones
            else "",
        )

        return all_points

    async def _fetch_all_meters(self):
        data = await self._api_get(DATA_ENDPOINT)
        if not data.get("response"):
            raise EnergaConnectionError("Empty response in fetch_all_meters")

        meters_found = []
        for mp in data["response"].get("meterPoints", []):
            # Original v4.0.9 logic: find matching top-level agreementPoint
            ag = next(
                (
                    a
                    for a in data["response"].get("agreementPoints", [])
                    if a.get("id") == mp.get("id")
                ),
                {},
            )
            if not ag and data["response"].get("agreementPoints"):
                ag = data["response"]["agreementPoints"][0]

            # Check nested agreementPoints for PPE if not in top-level
            nested_ag = mp.get("agreementPoints", [])
            if nested_ag and nested_ag[0].get("code"):
                ppe = nested_ag[0].get("code")
            else:
                ppe = ag.get("code") or mp.get("ppe") or mp.get("dev") or "Unknown"

            serial = mp.get("dev") or mp.get("meterNumber") or "Unknown"

            # Address: from agreement, or use meter name as fallback
            address = ag.get("address")
            if not address and mp.get("name") and mp.get("name") != serial:
                address = mp.get("name")

            # Contract date from top-level agreement (dealer.start)
            c_date = None
            try:
                start_ts = ag.get("dealer", {}).get("start")
                if start_ts:
                    c_date = datetime.fromtimestamp(int(start_ts) / 1000).date()
            except (ValueError, TypeError, OSError):
                pass

            meter_obj = {
                "meter_point_id": mp.get("id"),
                "ppe": ppe,
                "meter_serial": serial,
                "tariff": mp.get("tariff"),
                "address": address,
                "contract_date": c_date,
                "daily_pobor": None,
                "daily_produkcja": None,
                "total_plus": None,
                "total_minus": None,
                "total_plus_1": None,
                "total_plus_2": None,
                "total_minus_1": None,
                "total_minus_2": None,
                "obis_plus": None,
                "obis_minus": None,
                "zone_count": 1,
            }

            # Sum all A+ and A- zones; detect multi-zone tariffs (G12w)
            total_plus_sum = 0.0
            total_minus_sum = 0.0
            zone_numbers_seen = set()
            for m in mp.get("lastMeasurements", []):
                zone_name = m.get("zone", "")
                value = float(m.get("value", 0))
                if "A+" in zone_name:
                    total_plus_sum += value
                    if "strefa 1" in zone_name:
                        meter_obj["total_plus_1"] = value
                        zone_numbers_seen.add(1)
                    elif "strefa 2" in zone_name:
                        meter_obj["total_plus_2"] = value
                        zone_numbers_seen.add(2)
                if "A-" in zone_name:
                    total_minus_sum += value
                    if "strefa 1" in zone_name:
                        meter_obj["total_minus_1"] = value
                    elif "strefa 2" in zone_name:
                        meter_obj["total_minus_2"] = value

            if total_plus_sum > 0:
                meter_obj["total_plus"] = total_plus_sum
            if total_minus_sum > 0:
                meter_obj["total_minus"] = total_minus_sum

            # Detect zone count from lastMeasurements
            if len(zone_numbers_seen) > 1:
                meter_obj["zone_count"] = len(zone_numbers_seen)
                _LOGGER.info(
                    "Meter %s: multi-zone tariff detected (%s), %d zones",
                    serial,
                    mp.get("tariff"),
                    meter_obj["zone_count"],
                )

            for obj in mp.get("meterObjects", []):
                if obj.get("obis", "").startswith("1-0:1.8.0"):
                    meter_obj["obis_plus"] = obj.get("obis")
                elif obj.get("obis", "").startswith("1-0:2.8.0"):
                    meter_obj["obis_minus"] = obj.get("obis")
            meters_found.append(meter_obj)
        return meters_found

    async def _fetch_chart(
        self, meter_id: str, obis: str, timestamp: int,
        zone_index: int | None = None, include_timestamps: bool = False,
    ) -> list:
        """Fetch chart data for a meter.

        Args:
            meter_id: Meter point ID
            obis: OBIS code (e.g. 1-0:1.8.0*255)
            timestamp: Day timestamp in milliseconds
            zone_index: None=sum all zones, 0=zone 1, 1=zone 2
            include_timestamps: If True, return list of (value, tm_ms) tuples
                               instead of just values. Used for statistics
                               to correctly handle DST transitions (#26).
        """
        params = {
            "meterPoint": meter_id,
            "type": "DAY",
            "meterObject": obis,
            "mainChartDate": str(timestamp),
        }
        # Only add token if it exists, otherwise rely on cookies
        if self._token:
            params["token"] = self._token
        try:
            data = await self._api_get(CHART_ENDPOINT, params=params)
            results = []
            for p in data["response"]["mainChart"]:
                zones = p.get("zones", [])
                if zone_index is not None:
                    # Specific zone
                    val = zones[zone_index] if zone_index < len(zones) else None
                    val = val or 0.0
                else:
                    # Sum all zones (total)
                    val = sum(z or 0.0 for z in zones)

                if include_timestamps:
                    # Return (value, timestamp_ms) for DST-safe mapping
                    tm_ms = int(p.get("tm", 0))
                    results.append((val, tm_ms))
                else:
                    results.append(val)
            return results
        except EnergaTokenExpiredError:
            raise  # Propagate to coordinator for re-login
        except Exception as e:
            _LOGGER.error("Error fetching chart for %s: %s", meter_id, e)
            return []

    async def _api_get(self, path, params=None):
        for attempt in range(2):
            # Recover from closed session
            if self._session.closed:
                _LOGGER.warning(
                    "Session closed (attempt %d), creating new session and re-logging in",
                    attempt + 1,
                )
                self._session = self._create_session_fn()
                await self.async_login()

            url = f"{BASE_URL}{path}"

            # Build params INSIDE the loop so that after re-login
            # the fresh token is used (fixes stale token on retry)
            final_params = params.copy() if params else {}
            if self._token and "token" not in final_params:
                final_params["token"] = self._token

            try:
                async with self._session.get(
                    url, headers=HEADERS, params=final_params
                ) as resp:
                    if resp.status in (401, 403):
                        if attempt == 0:
                            _LOGGER.debug(
                                "Token expired (HTTP %d), re-logging in", resp.status
                            )
                            await self.async_login()
                            continue
                        raise EnergaTokenExpiredError(
                            f"API returned {resp.status} for {url}"
                        )
                    resp.raise_for_status()
                    data = await resp.json()

                    # Capture API warnings/errors for HA notifications
                    api_warning = data.get("warning") if isinstance(data, dict) else None
                    api_error = data.get("error") if isinstance(data, dict) else None
                    if api_warning and api_warning != self._api_warning:
                        self._api_warning = api_warning
                        _LOGGER.warning("Energa API warning: %s", api_warning)
                        if self._hass:
                            from homeassistant.components import persistent_notification
                            persistent_notification.async_create(
                                self._hass,
                                str(api_warning),
                                title="Energa: Komunikat",
                                notification_id="energa_api_warning",
                            )
                    if api_error and api_error != self._api_error:
                        self._api_error = api_error
                        _LOGGER.error("Energa API error: %s", api_error)
                        if self._hass:
                            from homeassistant.components import persistent_notification
                            persistent_notification.async_create(
                                self._hass,
                                str(api_error),
                                title="Energa: Błąd API",
                                notification_id="energa_api_error",
                            )

                    return data
            except (aiohttp.ClientError, RuntimeError) as err:
                if attempt == 0 and (
                    self._session.closed or "Session is closed" in str(err)
                ):
                    _LOGGER.warning("Request failed (session issue: %s), retrying", err)
                    continue
                raise EnergaConnectionError(str(err)) from err
        # Should not reach here, but safety net
        raise EnergaConnectionError("Max retries exceeded in _api_get")

    def set_energa24_refresh_token(self, token: str):
        """Set the refresh token for Energa24 dynamic pricing."""
        if token:
            token = token.strip().strip('"').strip("'")
        self._energa24_refresh_token = token or None

    async def _get_energa24_session(self) -> aiohttp.ClientSession:
        """Get or create a dedicated session for Energa24 API (separate cookies)."""
        if self._energa24_session is None or self._energa24_session.closed:
            self._energa24_session = aiohttp.ClientSession()
        return self._energa24_session

    async def async_close_energa24_session(self):
        """Close the dedicated Energa24 session."""
        if self._energa24_session and not self._energa24_session.closed:
            await self._energa24_session.close()
            self._energa24_session = None

    def set_energa24_token_updated_callback(self, callback):
        """Register a callback to persist refreshed tokens to config entry."""
        self._energa24_token_updated_cb = callback

    async def async_refresh_energa24_token(self) -> str | None:
        """Refresh the Energa24 access token using the refresh token.

        Handles token rotation: if Keycloak returns a new refresh_token,
        persists it via the registered callback so manual re-entry is
        never needed.
        """
        if not self._energa24_refresh_token:
            _LOGGER.debug("No Energa24 refresh token set")
            return None

        # Check if current token is still valid (with 30s buffer)
        if self._energa24_access_token and time.time() < self._energa24_token_expires - 30:
            return self._energa24_access_token

        _LOGGER.debug("Refreshing Energa24 access token")
        token_url = "https://24.energa.pl/auth/realms/Energa-Selfcare/protocol/openid-connect/token"
        token = self._energa24_refresh_token.strip().strip('"').strip("'")
        _LOGGER.info(
            "Energa24 token refresh: length=%d, prefix=%s..., suffix=%s",
            len(token),
            token[:25] if len(token) > 25 else token,
            token[-10:] if len(token) > 10 else "",
        )
        data = {
            "client_id": "energa-selfcare",
            "grant_type": "refresh_token",
            "refresh_token": token,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

        try:
            # Use a FRESH session for token refresh — never reuse the
            # shared _energa24_session whose cookies (server-cookie, etc.)
            # can cause Keycloak to reject the request.
            async with aiohttp.ClientSession() as token_session:
                async with token_session.post(
                    token_url, data=data, headers=headers
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        self._energa24_access_token = body.get("access_token")
                        expires_in = body.get("expires_in", 300)
                        self._energa24_token_expires = time.time() + float(expires_in)
                        _LOGGER.info("Energa24 access token refreshed (len=%d)", len(self._energa24_access_token or ""))

                        # Token rotation: persist new refresh_token if issued
                        new_refresh = body.get("refresh_token")
                        if new_refresh and new_refresh != token:
                            _LOGGER.info(
                                "Energa24: refresh token rotated, persisting new token"
                            )
                            self._energa24_refresh_token = new_refresh
                            if self._energa24_token_updated_cb:
                                try:
                                    self._energa24_token_updated_cb(new_refresh)
                                except Exception as cb_err:
                                    _LOGGER.warning(
                                        "Failed to persist rotated token: %s", cb_err
                                    )

                        return self._energa24_access_token
                    else:
                        error_text = await resp.text()
                        _LOGGER.warning(
                            "Energa24 token refresh: HTTP %d — %s",
                            resp.status,
                            error_text[:500],
                        )
                        return None
        except (aiohttp.ClientError, OSError) as err:
            _LOGGER.warning("Energa24 token refresh network error: %s", err)
            return None
        except Exception as err:
            _LOGGER.error("Error refreshing Energa24 token: %s", err)
            return None
        return None

    async def async_discover_energa24_ids(self) -> dict | None:
        """Auto-discover account_id and price_list_id from Energa24 API.

        Uses the configured refresh token to obtain an access token,
        then queries the API to find the user's account and active
        dynamic price list. Returns dict with account_id and
        price_list_id, or None on failure.
        """
        access_token = await self.async_refresh_energa24_token()
        if not access_token:
            _LOGGER.warning("Energa24: cannot discover IDs — token refresh failed")
            return None

        api_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "KeycloakId": self._extract_keycloak_id(access_token),
            "X-Client-Type": "WEB",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://24.energa.pl/",
            "Origin": "https://24.energa.pl",
        }

        e24_session = await self._get_energa24_session()

        # Step 1: Discover account_id from /api/accounts
        account_id = None
        try:
            async with e24_session.get(
                "https://24.energa.pl/api/accounts", headers=api_headers
            ) as resp:
                if resp.status == 200:
                    accounts = await resp.json()
                    if accounts and isinstance(accounts, list) and len(accounts) > 0:
                        account_id = str(accounts[0].get("id"))
                        _LOGGER.info(
                            "Energa24: auto-discovered account_id=%s", account_id
                        )
                else:
                    _LOGGER.warning(
                        "Energa24: /api/accounts returned HTTP %d", resp.status
                    )
        except Exception as err:
            _LOGGER.warning("Energa24: failed to query /api/accounts: %s", err)

        if not account_id:
            _LOGGER.error("Energa24: could not discover account_id")
            return None

        # Step 2: Discover price_list_id from dynamic offer list
        price_list_id = None
        try:
            async with e24_session.get(
                f"https://24.energa.pl/api/accounts/{account_id}/price-list-dynamic-offer",
                headers=api_headers,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        # Each item has an 'id' field
                        price_list_id = str(data[0].get("id"))
                        _LOGGER.info(
                            "Energa24: auto-discovered price_list_id=%s",
                            price_list_id,
                        )
                    elif isinstance(data, dict):
                        # Some API versions return a dict
                        price_list_id = str(data.get("id") or "")
                else:
                    _LOGGER.warning(
                        "Energa24: price-list-dynamic-offer returned HTTP %d",
                        resp.status,
                    )
        except Exception as err:
            _LOGGER.warning(
                "Energa24: failed to query price-list-dynamic-offer: %s", err
            )

        if not price_list_id:
            _LOGGER.error("Energa24: could not discover price_list_id")
            return None

        # Store discovered IDs
        self._energa24_account_id = account_id
        self._energa24_price_list_id = price_list_id

        return {"account_id": account_id, "price_list_id": price_list_id}

    async def async_validate_energa24(self) -> bool:
        """Validate Energa24 configuration by testing a price fetch."""
        if not self._energa24_refresh_token:
            return False
        prices = await self.async_get_dynamic_prices()
        return prices is not None and len(prices) > 0

    def set_energa24_ids(self, account_id: str, price_list_id: str):
        """Set Energa24 account and price list IDs from config."""
        self._energa24_account_id = account_id
        self._energa24_price_list_id = price_list_id

    @staticmethod
    def _extract_keycloak_id(access_token: str) -> str:
        """Extract 'sub' (KeycloakId) from JWT access token."""
        try:
            parts = access_token.split(".")
            if len(parts) != 3:
                return ""
            payload_b64 = parts[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload = base64.urlsafe_b64decode(payload_b64)
            data = json.loads(payload)
            return data.get("sub", "")
        except Exception:
            return ""

    async def async_get_dynamic_prices(self) -> list | None:
        """Fetch dynamic prices from Energa24 using configured IDs."""
        access_token = await self.async_refresh_energa24_token()
        if not access_token:
            return None

        if not self._energa24_account_id or not self._energa24_price_list_id:
            _LOGGER.warning("Energa24 account_id or price_list_id not configured")
            return None

        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        url = f"https://24.energa.pl/api/accounts/{self._energa24_account_id}/price-list-dynamic-offer/{self._energa24_price_list_id}/list"
        params = {"localDateFrom": today, "localDateTo": tomorrow}

        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "KeycloakId": self._extract_keycloak_id(access_token),
            "X-Client-Type": "WEB",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://24.energa.pl/",
            "Origin": "https://24.energa.pl",
        }

        try:
            e24_session = await self._get_energa24_session()
            async with e24_session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    offers = data.get("dynamicOffers", [])
                    _LOGGER.info("Energa24: fetched %d price slots", len(offers))
                    return offers
                else:
                    error_text = await resp.text()
                    _LOGGER.warning(
                        "Energa24 API returned HTTP %d: %s",
                        resp.status,
                        error_text[:200],
                    )
                    return None
        except Exception as err:
            _LOGGER.error("Error fetching Energa24 dynamic prices: %s", err)
            return None
