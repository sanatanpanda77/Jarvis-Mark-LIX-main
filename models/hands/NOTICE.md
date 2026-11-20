# Hand models (plugins/_hands.py)

Both come unchanged from the OpenCV Model Zoo (https://github.com/opencv/opencv_zoo),
converted there from Google's MediaPipe Hands, and run in OpenCV's own DNN module on
this computer only: no picture of a hand leaves it. Apache License 2.0
(`LICENSE-Apache-2.0.txt`). The pre- and post-processing in plugins/_hands.py follows
the Model Zoo's reference code (mp_palmdet.py, mp_handpose.py), Apache License 2.0.

- `palm_detection_mediapipe_2023feb.onnx` — MediaPipe palm detector, 192x192.
  sha256 `78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c`
- `handpose_estimation_mediapipe_2023feb.onnx` — MediaPipe hand landmarks (21 points,
  handedness), 224x224.
  sha256 `db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c`
