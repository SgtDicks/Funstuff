#include <Servo.h>

// Create an array for 6 servos
Servo servos[6];

// Define the pins for each servo
const int servoPins[6] = {3, 5, 6, 9, 10, 11};

void setup() {
  Serial.begin(9600);
  
  // Attach each servo and initialize at 90° (neutral)
  for (int i = 0; i < 6; i++) {
    servos[i].attach(servoPins[i]);
    servos[i].write(90);
  }
  
  Serial.println("Servo control ready. Send commands in format: servo_number,angle");
}

void loop() {
  if (Serial.available() > 0) {
    String input = Serial.readStringUntil('\n');
    input.trim();

    int commaIndex = input.indexOf(',');
    if (commaIndex == -1) {
      Serial.println("Invalid format. Use: servo_number,angle");
      return;
    }

    int servoNum = input.substring(0, commaIndex).toInt();
    int commandAngle = input.substring(commaIndex + 1).toInt();

    // Validate servo number and angle range
    if (servoNum < 1 || servoNum > 6 || commandAngle < 0 || commandAngle > 180) {
      Serial.println("Servo number must be 1-6 and angle 0-180.");
      return;
    }

    int outputAngle = commandAngle;
    
    // Apply calibration mapping for the eye servos (servo 1 and servo 2)
    if (servoNum == 1) {
      // For Servo 1 (eye): 0 -> closed=69, 180 -> open=154.
      outputAngle = map(commandAngle, 0, 180, 69, 154);
    } else if (servoNum == 2) {
      // For Servo 2 (eye): 0 -> open=154, 180 -> closed=69.
      outputAngle = map(commandAngle, 0, 180, 154, 69);
    }
    // For servos 3-6, the command is used directly (or add mapping as needed).

    servos[servoNum - 1].write(outputAngle);

    Serial.print("Servo ");
    Serial.print(servoNum);
    Serial.print(" set to ");
    Serial.print(outputAngle);
    Serial.println(" degrees.");
  }
}
