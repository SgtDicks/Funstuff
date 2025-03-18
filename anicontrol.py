import tkinter as tk
from tkinter import ttk
import serial
import threading
import time
from serial.tools import list_ports

class ServoControllerApp:
    def __init__(self, master):
        self.master = master
        self.master.title("Servo Controller")
        
        self.ser = None
        self.running = False
        
        # Calibrated slider ranges for each servo:
        # Servo 1: Top Eyelid Left, Servo 2: Top Eyelid Right,
        # Servo 3: Bottom Eyelid Left, Servo 4: Bottom Eyelid Right,
        # Servo 5: Eye Vertical, Servo 6: Eye Horizontal.
        self.servo_ranges = [
            (0, 110),    # Servo 1 (Top Eyelid Left)
            (0, 110),    # Servo 2 (Top Eyelid Right)
            (81, 112),   # Servo 3 (Bottom Eyelid Left)
            (0, 180),    # Servo 4 (Bottom Eyelid Right)
            (58, 135),   # Servo 5 (Eye Vertical)
            (50, 120)    # Servo 6 (Eye Horizontal)
        ]
        
        # Define which servos are reversed.
        # For example, if Servo 2 is mounted reversed, mark it as True.
        self.servo_reversed = [False, True, False, False, False, False]
        
        # --- Connection Frame ---
        connection_frame = ttk.LabelFrame(master, text="Connection")
        connection_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        
        ttk.Label(connection_frame, text="COM Port:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.com_port_combo = ttk.Combobox(connection_frame, values=[], state="readonly", width=10)
        self.com_port_combo.grid(row=0, column=1, padx=5, pady=5)
        self.refresh_button = ttk.Button(connection_frame, text="Refresh", command=self.refresh_com_ports)
        self.refresh_button.grid(row=0, column=2, padx=5, pady=5)
        self.refresh_com_ports()  # Populate COM ports initially
        
        ttk.Label(connection_frame, text="Baud Rate:").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.baud_entry = ttk.Entry(connection_frame, width=10)
        self.baud_entry.grid(row=1, column=1, padx=5, pady=5)
        self.baud_entry.insert(0, "9600")
        
        self.connect_button = ttk.Button(connection_frame, text="Connect", command=self.connect_serial)
        self.connect_button.grid(row=2, column=0, columnspan=3, padx=5, pady=5)
        
        # --- Servo Control Frame ---
        control_frame = ttk.LabelFrame(master, text="Servo Controls")
        control_frame.grid(row=1, column=0, padx=10, pady=10, sticky="ew")
        
        # Servo names
        self.servo_names = [
            "Top Eyelid Left",     # Servo 1
            "Top Eyelid Right",    # Servo 2
            "Bottom Eyelid Left",  # Servo 3
            "Bottom Eyelid Right", # Servo 4
            "Eye Vertical",        # Servo 5
            "Eye Horizontal"       # Servo 6
        ]
        
        # Lists to store slider widgets, value labels, and send buttons
        self.servo_sliders = []
        self.servo_value_labels = []
        self.send_buttons = []
        
        # Create a slider for each servo using its calibrated range.
        for i, name in enumerate(self.servo_names):
            row = i
            ttk.Label(control_frame, text=f"{name} (Servo {i+1}):").grid(row=row, column=0, padx=5, pady=5, sticky="w")
            
            min_val, max_val = self.servo_ranges[i]
            slider = ttk.Scale(control_frame, from_=min_val, to=max_val, orient="horizontal",
                               command=lambda val, idx=i: self.update_slider_label(idx, val))
            slider.grid(row=row, column=1, padx=5, pady=5, sticky="ew")
            self.servo_sliders.append(slider)
            
            # Compute initial effective value:
            init_val = (min_val + max_val) // 2
            if self.servo_reversed[i]:
                init_effective = min_val + max_val - init_val
            else:
                init_effective = init_val
            
            value_label = ttk.Label(control_frame, text=str(init_effective))
            value_label.grid(row=row, column=2, padx=5, pady=5)
            self.servo_value_labels.append(value_label)
            
            slider.set(init_val)
            
            btn = ttk.Button(control_frame, text="Send", command=lambda idx=i: self.send_servo_command(idx))
            btn.grid(row=row, column=3, padx=5, pady=5)
            self.send_buttons.append(btn)
        
        control_frame.columnconfigure(1, weight=1)
        
        # --- Log Frame ---
        log_frame = ttk.LabelFrame(master, text="Serial Output")
        log_frame.grid(row=2, column=0, padx=10, pady=10, sticky="nsew")
        
        self.log_text = tk.Text(log_frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True)
        
        # --- Script Frame ---
        script_frame = ttk.LabelFrame(master, text="Script Commands")
        script_frame.grid(row=3, column=0, padx=10, pady=10, sticky="ew")
        
        ttk.Label(script_frame, text="Enter one command per line:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.script_text = tk.Text(script_frame, height=5)
        self.script_text.grid(row=1, column=0, padx=5, pady=5, sticky="ew")
        script_frame.columnconfigure(0, weight=1)
        
        self.run_script_button = ttk.Button(script_frame, text="Run Script", command=self.run_script_thread)
        self.run_script_button.grid(row=2, column=0, padx=5, pady=5)
        
        # --- Quick Commands Frame ---
        quick_frame = ttk.LabelFrame(master, text="Quick Commands")
        quick_frame.grid(row=4, column=0, padx=10, pady=10, sticky="ew")
        
        # Row 0: Top Eyelid controls
        ttk.Button(quick_frame, text="Raise Eyelids", command=self.quick_raise_eyelids).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(quick_frame, text="Lower Eyelids", command=self.quick_lower_eyelids).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(quick_frame, text="Blink", command=self.quick_blink).grid(row=0, column=2, padx=5, pady=5)
        
        # Row 1: Bottom Eyelid controls
        ttk.Button(quick_frame, text="Open Bottom Eyelid", command=self.quick_open_bottom_eyelid).grid(row=1, column=0, padx=5, pady=5)
        ttk.Button(quick_frame, text="Close Bottom Eyelid", command=self.quick_close_bottom_eyelid).grid(row=1, column=1, padx=5, pady=5)
        
        # Row 2: Eye Horizontal controls
        ttk.Button(quick_frame, text="Look Right", command=self.quick_look_right).grid(row=2, column=0, padx=5, pady=5)
        ttk.Button(quick_frame, text="Look Left", command=self.quick_look_left).grid(row=2, column=1, padx=5, pady=5)
        
        # Row 3: Eye Vertical controls
        ttk.Button(quick_frame, text="Look Down", command=self.quick_look_down).grid(row=3, column=0, padx=5, pady=5)
        ttk.Button(quick_frame, text="Look Up", command=self.quick_look_up).grid(row=3, column=1, padx=5, pady=5)
        
        master.grid_rowconfigure(2, weight=1)
        master.grid_columnconfigure(0, weight=1)
    
    def refresh_com_ports(self):
        ports = list_ports.comports()
        port_list = [port.device for port in ports]
        self.com_port_combo['values'] = port_list
        if port_list:
            self.com_port_combo.current(0)
        else:
            self.com_port_combo.set("No COM ports")
    
    def update_slider_label(self, idx, value):
        raw_value = int(float(value))
        min_val, max_val = self.servo_ranges[idx]
        if self.servo_reversed[idx]:
            effective_value = min_val + max_val - raw_value
        else:
            effective_value = raw_value
        try:
            self.servo_value_labels[idx].config(text=str(effective_value))
        except IndexError:
            self.log(f"Error: index {idx} out of range in update_slider_label")
    
    def connect_serial(self):
        if self.ser is None:
            com_port = self.com_port_combo.get()
            baud_rate = int(self.baud_entry.get())
            try:
                self.ser = serial.Serial(com_port, baud_rate, timeout=1)
                self.running = True
                self.log(f"Connected to {com_port} at {baud_rate} baud.")
                self.connect_button.config(text="Disconnect")
                threading.Thread(target=self.read_serial, daemon=True).start()
            except Exception as e:
                self.log("Error connecting: " + str(e))
        else:
            self.disconnect_serial()
    
    def disconnect_serial(self):
        if self.ser:
            self.running = False
            self.ser.close()
            self.ser = None
            self.connect_button.config(text="Connect")
            self.log("Disconnected.")
    
    def send_servo_command(self, idx):
        if self.ser and self.ser.is_open:
            slider_value = int(float(self.servo_sliders[idx].get()))
            min_val, max_val = self.servo_ranges[idx]
            if self.servo_reversed[idx]:
                angle = min_val + max_val - slider_value
            else:
                angle = slider_value
            command = f"{idx+1},{angle}\n"
            try:
                self.ser.write(command.encode('utf-8'))
                self.log(f"Sent: {command.strip()}")
            except Exception as e:
                self.log("Error sending command: " + str(e))
        else:
            self.log("Serial port not connected.")
    
    def script_send(self, servo_idx, angle):
        """Send a command to a specific servo (using 0-based index) as part of a script or quick command.
           The provided angle is in the logical space and will be inverted if that servo is reversed."""
        if self.ser and self.ser.is_open:
            min_val, max_val = self.servo_ranges[servo_idx]
            if self.servo_reversed[servo_idx]:
                effective_angle = min_val + max_val - angle
            else:
                effective_angle = angle
            command = f"{servo_idx+1},{effective_angle}\n"
            try:
                self.ser.write(command.encode('utf-8'))
                self.log(f"Script sent: {command.strip()}")
            except Exception as e:
                self.log("Script error sending command: " + str(e))
        else:
            self.log("Serial port not connected (script command).")
    
    def run_script(self):
        script = self.script_text.get("1.0", tk.END).strip()
        if not script:
            self.log("No script to run.")
            return
        commands = script.splitlines()
        for line in commands:
            cmd = line.strip().lower()
            if not cmd:
                continue
            self.log(f"Executing script command: {cmd}")
            if cmd.startswith("wait "):
                try:
                    secs = float(cmd[5:])
                    time.sleep(secs)
                except Exception as e:
                    self.log("Error parsing wait command: " + str(e))
            elif cmd == "raise_eyelids":
                self.script_send(0, 110)  # Servo 1 open (logical)
                self.script_send(1, 0)    # Servo 2 open (logical; reversed yields 110)
            elif cmd == "lower_eyelids":
                self.script_send(0, 0)    # Servo 1 closed
                self.script_send(1, 110)  # Servo 2 closed (logical; reversed yields 0)
            elif cmd == "blink":
                self.script_send(0, 0)
                self.script_send(1, 110)
                time.sleep(0.2)
                self.script_send(0, 110)
                self.script_send(1, 0)
            elif cmd == "open_bottom_eyelid":
                self.script_send(2, 112)
            elif cmd == "close_bottom_eyelid":
                self.script_send(2, 81)
            elif cmd == "look_right":
                self.script_send(5, 50)
            elif cmd == "look_left":
                self.script_send(5, 120)
            elif cmd == "look_down":
                self.script_send(4, 58)
            elif cmd == "look_up":
                self.script_send(4, 125)
            elif cmd == "look_forward":
                self.script_send(5, 90)
                self.script_send(4, 130)
            else:
                self.log("Unknown script command: " + cmd)
        self.log("Script execution complete.")
    
    def run_script_thread(self):
        threading.Thread(target=self.run_script, daemon=True).start()
    
    def read_serial(self):
        while self.running and self.ser and self.ser.is_open:
            try:
                line = self.ser.readline().decode('utf-8').strip()
                if line:
                    self.log("Received: " + line)
            except Exception as e:
                self.log("Error reading: " + str(e))
            time.sleep(0.1)
    
    def log(self, message):
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
    
    # --- Quick Command Functions ---
    def quick_raise_eyelids(self):
        self.script_send(0, 110)  # Servo 1 open (logical)
        self.script_send(1, 0)    # Servo 2 open (logical; reversed yields 110)
    
    def quick_lower_eyelids(self):
        self.script_send(0, 0)    # Servo 1 closed
        self.script_send(1, 110)  # Servo 2 closed (logical; reversed yields 0)
    
    def blink_quick(self):
        self.script_send(0, 0)
        self.script_send(1, 110)
        time.sleep(0.2)
        self.script_send(0, 110)
        self.script_send(1, 0)
    
    def quick_blink(self):
        threading.Thread(target=self.blink_quick, daemon=True).start()
    
    def quick_open_bottom_eyelid(self):
        self.script_send(2, 112)
    
    def quick_close_bottom_eyelid(self):
        self.script_send(2, 81)
    
    def quick_look_right(self):
        self.script_send(5, 50)
    
    def quick_look_left(self):
        self.script_send(5, 120)
    
    def quick_look_down(self):
        self.script_send(4, 58)
    
    def quick_look_up(self):
        self.script_send(4, 135)

if __name__ == "__main__":
    root = tk.Tk()
    app = ServoControllerApp(root)
    root.mainloop()

