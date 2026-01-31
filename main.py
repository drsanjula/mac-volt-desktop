import flet as ft
try:
    import flet.charts as charts
except ImportError:
    charts = None
import subprocess
import re
import time
import threading
from collections import deque
import sys

# --- DATA CLASSES ---
# ... (rest of the file)

class PowerData:
    def __init__(self):
        self.power_source = "Unknown"
        self.battery_percent = 0
        self.charging_status = "Unknown"
        self.time_remaining = "Unknown"
        self.cycle_count = 0
        self.condition = "Checking..."
        self.max_capacity_percent = 100
        self.charger_wattage = 0
        self.charger_connected = False
        self.low_power_mode = False
        self.temperature = 0
        self.voltage = 0
        self.amperage = 0
        self.power_watts = 0
        self.adapter_voltage = 0
        self.adapter_current = 0
        self.current_capacity = 0
        self.design_capacity = 0
        
        # Power Mode: PERFORMANCE (0.5s), BALANCED (2s), ECO (5s)
        self.mode = "BALANCED"
        self.poll_interval = 2.0
        
        # History for graphs (last 100 points)
        self.power_history = deque([0.0] * 100, maxlen=100)
        
        # Syncing
        self.last_update = time.time()

class DataCollector(threading.Thread):
    def __init__(self, data_obj, lock, on_update_callback):
        super().__init__()
        self.data = data_obj
        self.lock = lock
        self.on_update = on_update_callback
        self.daemon = True
        self.running = True
        self.last_slow_check = 0

    def run_command(self, cmd_args):
        try:
            result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=5, shell=False)
            return result.stdout
        except Exception:
            return ""

    def run(self):
        while self.running:
            # 1. Collect Data via ioreg
            ioreg_out = self.run_command(["ioreg", "-w0", "-rn", "AppleSmartBattery"])
            
            with self.lock:
                # Basic Source
                ext_conn = '"ExternalConnected" = Yes' in ioreg_out or '"AppleRawExternalConnected" = Yes' in ioreg_out
                self.data.power_source = "AC Power" if ext_conn else "Battery"
                self.data.charger_connected = ext_conn
                
                # Percentage
                cur_cap_match = re.search(r'"CurrentCapacity"\s*=\s*(\d+)', ioreg_out)
                max_cap_match = re.search(r'"MaxCapacity"\s*=\s*(\d+)', ioreg_out)
                if cur_cap_match:
                    self.data.battery_percent = int(cur_cap_match.group(1))

                # Status
                is_charging = '"IsCharging" = Yes' in ioreg_out
                fully_charged = '"FullyCharged" = Yes' in ioreg_out
                if fully_charged: self.data.charging_status = "Fully Charged"
                elif is_charging: self.data.charging_status = "Charging"
                else: self.data.charging_status = "Discharging" if not ext_conn else "Connected"

                # Time Remaining
                t_match = re.search(r'"TimeRemaining"\s*=\s*(\d+)', ioreg_out)
                if t_match:
                    mins = int(t_match.group(1))
                    if mins == 65535: self.data.time_remaining = "Calculating..."
                    else: self.data.time_remaining = f"{mins // 60}h {mins % 60}m"

                # Temp & Voltage & Amperage
                t_match = re.search(r'"Temperature"\s*=\s*(\d+)', ioreg_out)
                if t_match: self.data.temperature = round((int(t_match.group(1)) / 10) - 273.15, 1)
                
                v_match = re.search(r'"Voltage"\s*=\s*(\d+)', ioreg_out)
                if v_match: self.data.voltage = int(v_match.group(1)) / 1000
                
                a_match = re.search(r'"InstantAmperage"\s*=\s*(-?\d+)', ioreg_out)
                if not a_match: a_match = re.search(r'"Amperage"\s*=\s*(-?\d+)', ioreg_out)
                if a_match:
                    amp = int(a_match.group(1))
                    if amp > 2**63: amp -= 2**64
                    self.data.amperage = amp
                
                self.data.power_watts = round(self.data.voltage * abs(self.data.amperage) / 1000, 2)
                self.data.power_history.append(self.data.power_watts)
                
                # Health % (Calculated)
                match = re.search(r'"CycleCount"\s*=\s*(\d+)', ioreg_out)
                if match: self.data.cycle_count = int(match.group(1))
                match = re.search(r'"DesignCapacity"\s*=\s*(\d+)', ioreg_out)
                if match: self.data.design_capacity = int(match.group(1))
                match = re.search(r'"AppleRawMaxCapacity"\s*=\s*(\d+)', ioreg_out)
                if match:
                    self.data.current_capacity = int(match.group(1))
                    if self.data.design_capacity > 0:
                        self.data.max_capacity_percent = round((self.data.current_capacity / self.data.design_capacity) * 100, 1)

                # Charger Details
                ad_match = re.search(r'"(?:AppleRaw)?AdapterDetails"\s*=\s*\{([^}]+)\}', ioreg_out)
                if ad_match:
                    ad_str = ad_match.group(1)
                    v_match = re.search(r'[ ,]\"?AdapterVoltage\"?[:=](\d+)', " " + ad_str)
                    if v_match: self.data.adapter_voltage = int(v_match.group(1)) / 1000
                    c_match = re.search(r'[ ,]\"?Current\"?[:=](\d+)', " " + ad_str)
                    if c_match: self.data.adapter_current = int(c_match.group(1))
                    w_match = re.search(r'[ ,]\"?Watts\"?[:=](\d+)', " " + ad_str)
                    if w_match: self.data.charger_wattage = int(w_match.group(1))

                self.data.last_update = time.time()

            # 3. Slow check for Condition & Low Power Mode (every 30s)
            if time.time() - self.last_slow_check > 30:
                prof_out = self.run_command(["system_profiler", "SPPowerDataType"])
                match = re.search(r'Condition:\s*(\w+)', prof_out)
                lpm_out = self.run_command(["pmset", "-g"])
                with self.lock:
                    if match: self.data.condition = match.group(1)
                    lpm_match = re.search(r'lowpowermode\s+(\d)', lpm_out)
                    self.data.low_power_mode = (lpm_match.group(1) == '1') if lpm_match else False
                self.last_slow_check = time.time()
            
            # Notify UI
            self.on_update()
            time.sleep(self.data.poll_interval)

# --- UI COMPONENTS ---

class MetricCard(ft.Container):
    def __init__(self, title, value, unit, icon, color=ft.Colors.BLUE_400):
        super().__init__()
        self.title_text = ft.Text(title, size=12, color=ft.Colors.GREY_500, weight=ft.FontWeight.BOLD)
        self.value_text = ft.Text(value, size=24, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)
        self.unit_text = ft.Text(unit, size=14, color=ft.Colors.GREY_400)
        self.icon_comp = ft.Icon(icon, color=color, size=30)
        
        self.content = ft.Row(
            [
                self.icon_comp,
                ft.Column(
                    [
                        self.title_text,
                        ft.Row([self.value_text, self.unit_text], alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.BASELINE, spacing=5),
                    ],
                    spacing=0,
                ),
            ],
            alignment=ft.MainAxisAlignment.START,
            spacing=15,
        )
        self.padding = 15
        self.bgcolor = ft.Colors.with_opacity(0.05, ft.Colors.WHITE)
        self.border_radius = 12
        self.width = 200

    def update_value(self, value, color=None):
        self.value_text.value = str(value)
        if color:
            self.value_text.color = color
        self.update()

def main(page: ft.Page):
    page.title = "Mac Volt Monitor"
    page.window_width = 900
    page.window_height = 700
    page.bgcolor = "#0B0E14"
    page.theme_mode = ft.ThemeMode.DARK
    page.window_resizable = False
    page.padding = 30

    # Data Initialization
    data = PowerData()
    lock = threading.Lock()
    
    # --- REFRESH UI ---
    def update_ui():
        try:
            with lock:
                # Update Cards
                power_card.update_value(f"{data.power_watts}", ft.Colors.GREEN_400 if data.amperage >= 0 else ft.Colors.YELLOW_400)
                volt_card.update_value(f"{data.voltage:.2f}")
                temp_card.update_value(f"{data.temperature}", ft.Colors.GREEN_400 if data.temperature < 40 else ft.Colors.RED_400)
                amp_card.update_value(f"{abs(data.amperage)}")
                
                # Update Battery Header
                batt_percent.value = f"{data.battery_percent}%"
                batt_status.value = f"{data.charging_status} • {data.time_remaining}"
                batt_progress.value = data.battery_percent / 100
                batt_progress.color = ft.Colors.GREEN_400 if data.battery_percent > 50 else (ft.Colors.YELLOW_400 if data.battery_percent > 20 else ft.Colors.RED_400)
                
                # Update Health Info
                health_val.value = f"{data.max_capacity_percent}%"
                cycle_val.value = f"{data.cycle_count}"
                cond_val.value = data.condition
                
                # Update Charger Info
                if data.charger_connected:
                    charger_watt_text.value = f"{data.charger_wattage}W"
                    charger_details.value = f"{data.adapter_voltage:.1f}V / {data.adapter_current}mA"
                    charger_panel.visible = True
                else:
                    charger_panel.visible = False
                
                # Update Line Chart if available
                if charts and chart:
                    chart.data_series[0].data_points = [
                        charts.LineChartDataPoint(i, val) for i, val in enumerate(data.power_history)
                    ]
                else:
                    chart_placeholder.value = f"Power History: {list(data.power_history)[-5:]}"
                
                # Metadata
                meta_text.value = f"Last Update: {time.strftime('%H:%M:%S')} | Mode: {data.mode}"
                
            page.update()
        except Exception:
            pass # UI not ready yet

    collector = DataCollector(data, lock, update_ui)

    # --- UI LAYOUT ---
    
    # Header Section
    header = ft.Row(
        [
            ft.Column([
                ft.Text("Mac Volt Monitor", size=32, weight=ft.FontWeight.BOLD, color=ft.Colors.CYAN_400),
                ft.Text("Native Desktop Visualization", color=ft.Colors.GREY_500),
            ]),
            ft.SegmentedButton(
                selected=["balanced"],
                allow_multiple_selection=False,
                on_change=lambda e: change_mode(e.data),
                segments=[
                    ft.Segment(value="perf", label=ft.Text("Perf")),
                    ft.Segment(value="balanced", label=ft.Text("Bal")),
                    ft.Segment(value="eco", label=ft.Text("Eco")),
                ],
            )
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN
    )

    def change_mode(mode_id):
        raw_id = mode_id.strip("{}'\"") # Flet passes stringified set
        with lock:
            if "perf" in raw_id:
                data.mode = "PERFORMANCE"
                data.poll_interval = 0.5
            elif "eco" in raw_id:
                data.mode = "ECO"
                data.poll_interval = 5.0
            else:
                data.mode = "BALANCED"
                data.poll_interval = 2.0
        update_ui()

    # Battery Progress Section
    batt_percent = ft.Text("0%", size=48, weight=ft.FontWeight.BOLD)
    batt_status = ft.Text("Discharging • Calculating...", color=ft.Colors.GREY_400)
    batt_progress = ft.ProgressBar(value=0, height=12, border_radius=6, color=ft.Colors.GREEN_400, bgcolor=ft.Colors.GREY_800)
    
    battery_section = ft.Container(
        content=ft.Column([
            ft.Row([batt_percent, ft.Icon(ft.Icons.BATTERY_CHARGING_FULL, size=40, color=ft.Colors.CYAN_400)], alignment=ft.MainAxisAlignment.START),
            batt_status,
            ft.Container(height=10),
            batt_progress,
        ]),
        padding=20,
        bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE),
        border_radius=20,
    )

    # Metrics Grid
    power_card = MetricCard("POWER FLOW", "0", "W", ft.Icons.FLASH_ON_ROUNDED, ft.Colors.AMBER)
    volt_card = MetricCard("VOLTAGE", "0.0", "V", ft.Icons.ELECTRIC_BOLT)
    temp_card = MetricCard("TEMP", "0.0", "°C", ft.Icons.THERMOSTAT)
    amp_card = MetricCard("CURRENT", "0", "mA", ft.Icons.SPEED)

    metrics_grid = ft.Row([power_card, volt_card, temp_card, amp_card], spacing=20, wrap=True)

    # Health & Charger Details (Side by Side)
    health_val = ft.Text("100%", size=16, weight=ft.FontWeight.BOLD)
    cycle_val = ft.Text("0", size=16, weight=ft.FontWeight.BOLD)
    cond_val = ft.Text("Normal", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_400)
    
    health_panel = ft.Container(
        content=ft.Column([
            ft.Text("HEALTH INSIGHTS", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.CYAN_400),
            ft.Divider(height=10, color=ft.Colors.GREY_800),
            ft.Row([ft.Text("Capacity: ", color=ft.Colors.GREY_500), health_val]),
            ft.Row([ft.Text("Cycles: ", color=ft.Colors.GREY_500), cycle_val]),
            ft.Row([ft.Text("Condition: ", color=ft.Colors.GREY_500), cond_val]),
        ]),
        padding=20,
        bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE),
        border_radius=15,
        expand=1
    )

    charger_watt_text = ft.Text("65W", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER)
    charger_details = ft.Text("20V / 3250mA", size=14, color=ft.Colors.GREY_400)
    
    charger_panel = ft.Container(
        content=ft.Column([
            ft.Text("CHARGER ATTACHED", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER),
            ft.Divider(height=10, color=ft.Colors.GREY_800),
            ft.Row([ft.Icon(ft.Icons.POWER_ROUNDED, color=ft.Colors.AMBER, size=20), charger_watt_text]),
            charger_details,
        ]),
        padding=20,
        bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.BLACK), # Subtle hint
        border=ft.Border.all(1, ft.Colors.with_opacity(0.1, ft.Colors.AMBER)),
        border_radius=15,
        expand=1,
        visible=False
    )

    side_panels = ft.Row([health_panel, charger_panel], spacing=20)

    # Chart Section (using flet-charts)
    if charts:
        chart = charts.LineChart(
            data_series=[
                charts.LineChartData(
                    data_points=[],
                    stroke_width=3,
                    color=ft.Colors.CYAN_400,
                    curved=True,
                    below_line_bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.CYAN_400),
                    below_line_gradient=ft.LinearGradient(
                        begin=ft.Alignment.TOP_CENTER,
                        end=ft.Alignment.BOTTOM_CENTER,
                        colors=[ft.Colors.with_opacity(0.2, ft.Colors.CYAN_400), ft.Colors.TRANSPARENT],
                    ),
                )
            ],
            border=ft.Border.all(1, ft.Colors.GREY_800),
            horizontal_grid_lines=charts.ChartGridLines(interval=5, color=ft.Colors.with_opacity(0.1, ft.Colors.GREY_400), width=1),
            vertical_grid_lines=charts.ChartGridLines(interval=10, color=ft.Colors.with_opacity(0.1, ft.Colors.GREY_400), width=1),
            left_axis=charts.ChartAxis(labels_size=40, title=ft.Text("Watts"), title_size=30),
            bottom_axis=charts.ChartAxis(title=ft.Text("Last 100 Samples"), title_size=30),
            expand=True,
        )
        chart_content = chart
    else:
        chart = None
        chart_placeholder = ft.Text("Chart support missing (install flet-charts)", color=ft.Colors.GREY_500, size=12)
        chart_content = ft.Container(content=chart_placeholder, alignment=ft.Alignment.CENTER)

    chart_section = ft.Container(
        content=ft.Column([
            ft.Text("POWER CONSUMPTION TREND", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_500),
            ft.Container(height=200, content=chart_content),
        ]),
        padding=20,
        bgcolor=ft.Colors.with_opacity(0.02, ft.Colors.WHITE),
        border_radius=20,
    )

    meta_text = ft.Text("Initializing collector...", color=ft.Colors.GREY_600, size=11)

    # Assemble Page
    page.add(
        header,
        ft.Container(height=20),
        battery_section,
        ft.Container(height=10),
        metrics_grid,
        ft.Container(height=10),
        side_panels,
        ft.Container(height=10),
        chart_section,
        ft.Container(height=10),
        ft.Row([meta_text], alignment=ft.MainAxisAlignment.CENTER)
    )
    
    # Start collector after UI is built
    collector.start()

if __name__ == "__main__":
    ft.run(main)
