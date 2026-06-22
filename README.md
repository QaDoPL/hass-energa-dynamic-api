<div align="center">
  <img src="logo.png" alt="Energa My Meter API Logo" width="300"/>
</div>

<h1 align="center">Energa My Meter API Integration for Home Assistant</h1>


![GitHub Release](https://img.shields.io/github/v/release/ergo5/hass-energa-my-meter-api)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
![API](https://img.shields.io/badge/data_source-Native_API-blue?logo=fastapi)

🇵🇱 This integration is designed for customers of **Energa Operator** — a regional electricity distributor serving **northern Poland** (Pomorze, Warmia-Mazury, Kujawsko-Pomorskie).

A robust integration for **Energa Operator** in Home Assistant that communicates via the **native API** — **not web scraping**. It retrieves data directly from the "Mój Licznik" API and integrates seamlessly with the **Energy Dashboard**. Features **self-healing history import**, **automatic cost calculation**, and correct cumulative statistics.

---

## 📡 Native API

This integration communicates directly with Energa's **native REST API** (`api-mojlicznik.energa-operator.pl`).

*   🔗 **Direct API communication** — lightweight JSON responses, no HTML parsing
*   🔐 **Token-based authentication** with automatic session refresh
*   📦 **Structured data** — precise meter readings and hourly charts straight from the source
*   🔄 **Stable interface** — based on a structured API backend, not website layout

> [!TIP]
> For technical details about the API endpoints, see [ENERGA_API_REFERENCE.md](docs/ENERGA_API_REFERENCE.md).

---

## ✨ Key Features

*   **📡 Native API:** Direct communication with Energa's REST API — no HTML parsing, stable JSON interface.
*   **📊 Energy Dashboard Ready:** Dedicated sensors (`Panel Energia`) designed specifically for correct statistics.
*   **💰 Automatic Cost Calculation:** Calculates energy costs in PLN based on configured prices.
*   **🛡️ Forward-From-Zero Statistics:** Monotonically increasing sums with spike guard protection.
*   **⚡ Hourly Granularity:** Precise hourly consumption/production tracking.
*   **🔌 Multi-Zone Tariffs (G12/G12w):** Automatic detection of two-zone meters with separate peak/off-peak tracking and per-zone live meter readings.
*   **🛠️ Auto-Repair (Self-Healing):** The "Download History" feature automatically fixes gaps and corrupted data.
*   **🔍 OBIS Auto-Detect:** Automatically identifies usage (1.8.0) and production (2.8.0).
*   **⚖️ Prosumer Balance:** Real-time net metering balance with configurable baselines and coefficient.
*   **🏷️ Price Sensors:** Exposes configured energy prices as HA entities for use with "Use entity with current price" mode in Energy Dashboard.
*   **⚡ Energa24 Dynamic Pricing:** Full support for Energa24 hourly market prices with auto-discovery and automatic token rotation.

---

## 💰 Cost Calculation

The integration **automatically calculates energy costs** and displays them in the Energy Dashboard in **PLN (złoty)**.

**How it works:**
- When you configure energy prices (see below), the integration creates cost sensors
- Cost sensors: `*_koszt` (consumption), `*_rekompensata` (production compensation)
- These sensors work seamlessly with the Energy Dashboard to show costs alongside energy usage

> [!NOTE]
> **Two-zone tariffs** (G12, G12w, G12r) are fully supported with separate zone pricing. Three-zone tariffs (G13) are not currently supported.
>
> For **dynamic tariff** users: the `Energa Dynamiczna Cena` sensor provides live market prices. You can use the Energy Dashboard's **"Use entity with current price"** mode by selecting the `Cena Poboru` sensor as your price source, then manually linking the dynamic price sensor in automations.

---

## 📦 Installation

### Option 1: HACS (Recommended)
1.  Open **HACS** → **Integrations**.
2.  Search for **Energa My Meter**.
3.  Click **Install** and restart Home Assistant.

### Configuration
1.  Go to **Settings** -> **Devices & Services**.
2.  Add Integration -> Search for **Energa My Meter**.
3.  Login With your **Energa Mój Licznik** credentials.

---

## ⚙️ Price Configuration

To enable cost calculation, you must configure energy prices:

1. Go to **Settings** → **Devices & Services** → **Energa My Meter**
2. Click **Configure** (three dots menu)
3. Select **"Set Energy Prices"** (Ustaw Ceny Energii)
4. Enter your prices:

| Tariff | Field | Default (PLN/kWh) |
|---|---|---|
| **G11** (single-zone) | Import | 1.188 |
| **G12/G12w** zone 1 (peak) | Import Zone 1 | 1.2453 |
| **G12/G12w** zone 2 (off-peak) | Import Zone 2 | 0.5955 |
| All tariffs | Export | 0.95 |

> [!TIP]
> The options form automatically adapts to your tariff — two-zone meters (G12/G12w) will see zone-specific fields, single-zone meters (G11) will see a single import price.

---

## ⚡ Energa24 Dynamic Pricing

The integration supports **Energa24 dynamic hourly prices** for users on the dynamic tariff (`Taryfa Dynamiczna`). This automatically fetches current and next-day market prices, enabling smart load scheduling (heat pump, EV charging, battery storage).

### Setup (simplified — one field only)

1. Go to **Settings** → **Devices & Services** → **Energa My Meter** → **Configure**
2. Select **"Dynamic Pricing (Energa24)"**
3. Paste your **Refresh Token** (obtained once from your browser)

**How to get the Refresh Token:**
1. Open [24.energa.pl](https://24.energa.pl) in your browser and log in
2. Open **Developer Tools** (F12) → **Application** tab → **Local Storage**
3. Find the entry `refresh_token` under `https://24.energa.pl`
4. Copy its value and paste it into the integration

> [!NOTE]
> The **Account ID** and **Price List ID** are **auto-discovered** from the API — you no longer need to enter them manually.

### Automatic Token Rotation

When the Energa24 Keycloak server issues a new refresh token, the integration automatically persists it to your configuration. You will **never** need to manually update the token again, unless you explicitly log out of the Energa24 portal.

### Dynamic Price Sensor

| Sensor | Description |
|--------|-------------|
| `Energa Dynamiczna Cena` | Current 15-min slot gross price (PLN/kWh) |

**Extra attributes** include:
- `current_slot` — current time window (e.g. `14:00 - 14:15`)
- `today_min_price`, `today_max_price`, `today_avg_price` — daily stats
- `tomorrow_min_price`, `tomorrow_max_price`, `tomorrow_avg_price` — next-day stats (available after ~16:00)
- `cheapest_2h_window`, `most_expensive_2h_window` — best/worst 2-hour blocks
- `cheapest_hour`, `most_expensive_hour` — best/worst single hours

---

## ⚖️ Prosumer Balance

The **Bilans Prosumencki** sensor calculates your net metering balance in real time:

```
(export − baseline_export) × coefficient − (import − baseline_import)
```

- **Baselines:** Set in Options to match your meter readings at the start of the billing period (e.g., from your last prosumer invoice)
- **Coefficient:** Configured prosumer net-billing coefficient (default 0.8)
- **Positive value = surplus**, negative = consumed more than produced

### Price Sensors

Diagnostic sensors expose your configured prices as HA entities:

| Sensor | Description |
|--------|-------------|
| `Cena Poboru` / `Cena Poboru Strefa 1` / `Strefa 2` | Configured import price |
| `Cena Oddania` | Configured export compensation rate |
| `Współczynnik Prosumencki` | Current prosumer coefficient |

These can be used with the Energy Dashboard's **"Use entity with current price"** mode for dynamic cost visualization.

---

## 🧹 Clear Statistics

If your Energy Panel shows incorrect spikes (e.g., after major integration updates), use the **Clear Statistics** option:

1. Go to **Settings** → **Devices & Services** → **Energa My Meter** → **Configure**
2. Select **"Clear Energy Panel Statistics"**
3. Confirm — this removes all historical Energa statistics
4. Use **"Download History"** to re-import clean data

> [!WARNING]
> Clearing statistics is **irreversible**. The history needs to be re-downloaded afterwards.

---

## 📡 Available Sensors

The integration creates multiple sensors organized by function:

### Energy Dashboard Sensors (Panel Energia)
**Use these for the Energy Dashboard:**

| Sensor Name | Description | Purpose |
|-------------|-------------|---------|
| `Panel Energia Zużycie` | Cumulative consumption | Grid Consumption in Dashboard |
| `Panel Energia Produkcja` | Cumulative production | Return to Grid in Dashboard |
| `Panel Energia Zużycie Koszt` | Consumption cost (PLN) | Auto-created for cost tracking |
| `Panel Energia Produkcja Rekompensata` | Production compensation (PLN) | Auto-created for cost tracking |

#### Multi-Zone Sensors (auto-created for G12/G12w tariffs)

| Sensor Name | Description | Purpose |
|-------------|-------------|---------|
| `Panel Energia Strefa 1` | Peak zone consumption | Zone 1 tracking in Dashboard |
| `Panel Energia Strefa 2` | Off-peak zone consumption | Zone 2 tracking in Dashboard |
| `Panel Energia Strefa 1 Koszt` | Peak zone cost (PLN) | Zone 1 cost tracking |
| `Panel Energia Strefa 2 Koszt` | Off-peak zone cost (PLN) | Zone 2 cost tracking |
| `Panel Energia Produkcja Strefa 1` | Peak zone production | Zone 1 export tracking |
| `Panel Energia Produkcja Strefa 2` | Off-peak zone production | Zone 2 export tracking |
| `Panel Energia Produkcja Strefa 1 Rekompensata` | Peak zone export compensation (PLN) | Zone 1 export cost |
| `Panel Energia Produkcja Strefa 2 Rekompensata` | Off-peak zone export compensation (PLN) | Zone 2 export cost |

### Daily Sensors
| Sensor Name | Description |
|-------------|-------------|
| `Zużycie Dziś` | Today's consumption (kWh) |
| `Produkcja Dziś` | Today's production (kWh) |

### Meter State Sensors
| Sensor Name | Description |
|-------------|-------------|
| `Stan Licznika Import` | Total meter reading — consumption |
| `Stan Licznika Import Strefa 1` | Zone 1 (peak) total reading (G12/G12w only) |
| `Stan Licznika Import Strefa 2` | Zone 2 (off-peak) total reading (G12/G12w only) |
| `Stan Licznika Export` | Total meter reading — production |
| `Stan Licznika Export Strefa 1` | Zone 1 (peak) export total (G12/G12w prosumers) |
| `Stan Licznika Export Strefa 2` | Zone 2 (off-peak) export total (G12/G12w prosumers) |

### Prosumer & Price Sensors
| Sensor Name | Description |
|-------------|-------------|
| `Bilans Prosumencki` | Net metering balance: (export − baseline) × coeff − (import − baseline) |
| `Cena Poboru` / `Cena Poboru Strefa 1` / `Strefa 2` | Configured import price (PLN/kWh) |
| `Cena Oddania` | Configured export compensation rate (PLN/kWh) |
| `Współczynnik Prosumencki` | Current prosumer coefficient |

### Energa24 Dynamic Pricing Sensor
| Sensor Name | Description |
|-------------|-------------|
| `Energa Dynamiczna Cena` | Current 15-min slot gross price (PLN/kWh) with full daily/next-day stats |

### Metadata Sensors
| Sensor Name | Description |
|-------------|-------------|
| `Adres` | Installation address |
| `Taryfa` | Tariff type (e.g., G11, G12, G12w) |
| `PPE` | PPE identification number |
| `Numer Licznika` | Meter serial number |
| `Data Aktywacji` | Mój Licznik app activation date* |

*Only available for prosumer accounts

---

## 📊 Energy Dashboard Setup

To see correctly calculated statistics **and costs** in the Energy Dashboard, you MUST select the specific sensors labeled with **"(Panel Energia)"**.

### Step 1: Configure Grid Consumption

<img src="docs/energy_dashboard_config.png" alt="Energy Dashboard Configuration" width="400"/>

*Example configuration showing Panel Energia sensors with cost tracking*

| Dashboard Section | Correct Sensor | Cost Sensor |
| :--- | :--- | :--- |
| **Grid Consumption** (Pobór z sieci) | **Energa [ID] Panel Energia Zużycie** | **Energa [ID] Panel Energia Zużycie Cost** |
| **Return to Grid** (Oddawanie do sieci) | **Energa [ID] Panel Energia Produkcja** | **Energa [ID] Panel Energia Produkcja Cost** |

> [!IMPORTANT]
> **Do NOT use:**
> - `Energa Zużycie Dziś` or `Stan Licznika` for the Energy Dashboard
> - Only sensors marked **(Panel Energia)** are designed for statistics

### Step 2: Configure Cost Sensors

<img src="docs/energy_cost_config.png" alt="Cost Sensor Configuration" width="400"/>

*Configure cost tracking by selecting the matching cost sensor*

When adding energy sources to the Energy Dashboard:
1. Select the **Panel Energia** sensor for energy tracking
2. In the **cost** field, select the corresponding `*_cost` sensor
3. The cost sensor **must match** the energy sensor (e.g., `zuzycie` with `zuzycie_cost`)

> [!NOTE]
> **"Entity Unavailable" (Encja niedostępna)?**
> This is **NORMAL** for statistics sensors (`*_stats`, `*_cost`). They work in background for the Energy Dashboard and don't have a live "state" to display. **They will still work correctly.**

---

## 📅 History Import & Repair

Use this feature if you have missing data OR if you see incorrect spikes in your Energy Dashboard.

1.  Go to **Settings** -> **Devices & Services** -> **Energa My Meter** -> **Configure**.
2.  Select **"Download History"** (Pobierz Historię Danych).
3.  Choose a **Start Date** (e.g., 30 days ago).
4.  Click **Submit**.

**How it works:** The integration downloads fresh data from Energa and calculates clean, continuous statistics based on your current meter reading. This effectively **overwrites** any corrupted historical data, including cost data.

*The process happens in the background. Check logs for progress.*

---

## ⚠️ Limitations

- **Supported Tariffs:** G11 (single-zone) and two-zone tariffs (G12, G12w, G12r) are fully supported. Three-zone tariffs (G13) are not supported — if you need G13, please [open an issue](https://github.com/ergo5/hass-energa-my-meter-api/issues).
- **PLN Currency:** Cost calculation is in Polish złoty (PLN) only.
- **Statistics Sensors:** Panel Energia sensors may show as "Unavailable" in entity lists (this is normal — they work in Energy Dashboard).
- **Hourly Granularity:** Statistics are hourly — no sub-hour precision.
- **Energa24:** Token must be manually extracted once from browser (auto-rotated thereafter). Price data is fetched on the coordinator's hourly cycle.

---

## 🐛 Troubleshooting

### "Token expired" / Authentication Issues

If you see errors like "Token expired, attempting re-login" or frequent authentication failures:

**Solution:** Reinstall and re-add the integration

1. **Update to v4.2.0 or newer** (skip if already on latest):
   - Open **HACS** → **Integrations** 
   - Find **Energa My Meter** → Click **Update** (or **Redownload**)
   - Restart Home Assistant

2. **Remove and re-add configuration**:
   - Go to **Settings** → **Devices & Services** → **Energa My Meter**
   - Click the **3 dots** → **Delete**
   - Add the integration again with your credentials

**Why this helps:** Older versions (before v4.0.9) didn't save the device token properly. Step 1 gets you the fixed code, step 2 saves a persistent token that prevents authentication conflicts.

### Energa24 Dynamic Price Not Updating

**Symptom:** `Energa Dynamiczna Cena` shows "unavailable" or stale values.

**Solutions:**
1. **Verify token is still valid:** Re-enter the refresh token in **Configure → Dynamic Pricing (Energa24)**. If you've logged out of the Energa24 portal, the token is invalidated.
2. **Check API connectivity:** The Energa24 token endpoint is separate from the Mój Licznik API. Temporary outages occur.
3. **Auto-discovery failed:** If you see "not configured" for Account ID, delete and re-enter your refresh token to trigger auto-discovery again.

> [!NOTE]
> The integration **automatically rotates** the refresh token when Keycloak issues a new one. If the token stops working entirely, you'll need to re-extract it from your browser (see Energa24 setup above).

### Bilans Prosumencki Shows Unexpected Values

1. **Set baselines correctly:** Go to **Configure → Set Energy Prices** and enter your meter readings from the **start of the billing period** as `balance_baseline_import` and `balance_baseline_export`.
2. **Verify coefficient:** The default is 0.8. Check your contract for the correct prosumer net-billing coefficient.
3. **Baselines are in kWh:** Enter the raw meter reading (e.g., 12345.6 kWh), not a difference.

### Sensors "Panel Energia" Missing?

- Check the **Diagnostic** entities section
- Enable "Show disabled entities" in entity list

### Cost Not Showing in Energy Dashboard?

1. **Verify prices are configured:** Settings → Energa My Meter → Configure → Set Energy Prices
2. **Check cost sensors exist:** Look for `*_cost` sensors in entity list
3. **Ensure correct mapping:** Cost sensor must match energy sensor (e.g., `zuzycie` with `zuzycie_cost`)

### Data Not Appearing in Energy Dashboard?

Ensure you selected the correct `(Panel Energia)` sensors, not the "Daily" or "State" sensors.

### About "Data Aktywacji" Sensor

This sensor shows the **activation date of the Mój Licznik mobile app**, not the contract signing date. It's only available for prosumer (producer-consumer) accounts and may not appear for regular consumer accounts.

---

## 📄 Changelog

See [CHANGELOG.md](CHANGELOG.md) for detailed version history.

---

### Disclaimer
This is a custom integration and is not affiliated with Energa Operator. Use at your own risk.
