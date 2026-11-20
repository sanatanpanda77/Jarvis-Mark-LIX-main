# Face models (plugins/face_lock.py)

Both come unchanged from the OpenCV Model Zoo (https://github.com/opencv/opencv_zoo)
and run in OpenCV's own DNN module (`cv2.FaceDetectorYN`, `cv2.FaceRecognizerSF`),
on this computer only: no picture of a face leaves it.

- `face_detection_yunet_2023mar.onnx` — **YuNet** face detector, MIT License
  (`LICENSE-YuNet-MIT.txt`), Copyright (c) 2020 Shiqi Yu.
  sha256 `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`
- `face_recognition_sface_2021dec_int8.onnx` — **SFace** face recognizer
  (int8-quantized), Apache License 2.0 (`LICENSE-SFace-Apache-2.0.txt`).
  Y. Zhong, W. Deng, J. Hu, D. Zhao, X. Li, D. Wen, "SFace: Sigmoid-Constrained
  Hypersphere Loss for Robust Face Recognition", IEEE TIP 2021.
  sha256 `2b0e941e6f16cc048c20aee0c8e31f569118f65d702914540f7bfdc14048d78a`
