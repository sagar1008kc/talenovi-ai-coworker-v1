// Local, free face detection and image similarity with Apple's Vision framework (no network).
//   vision_faces faces <image> [<image> ...]   -> JSON [{"path":..,"faces":[[x,y,w,h],..]}]  (top-left origin, 0-1)
//   vision_faces distance <imageA> <imageB>    -> JSON {"distance": d}  (feature-print distance; lower = more alike)
import Foundation
import Vision
import CoreImage

func cgImage(_ path: String) -> CGImage? {
    guard let ci = CIImage(contentsOf: URL(fileURLWithPath: path)) else { return nil }
    return CIContext().createCGImage(ci, from: ci.extent)
}

func faces(_ path: String) -> [[Double]] {
    guard let img = cgImage(path) else { return [] }
    let req = VNDetectFaceRectanglesRequest()
    try? VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
    return (req.results ?? []).map { o in
        let b = o.boundingBox
        return [Double(b.minX), Double(1.0 - b.maxY), Double(b.width), Double(b.height)]
    }
}

func featurePrint(_ path: String) -> VNFeaturePrintObservation? {
    guard let img = cgImage(path) else { return nil }
    let req = VNGenerateImageFeaturePrintRequest()
    try? VNImageRequestHandler(cgImage: img, options: [:]).perform([req])
    return req.results?.first as? VNFeaturePrintObservation
}

let args = CommandLine.arguments
func emit(_ obj: Any) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [])
    print(String(data: data, encoding: .utf8)!)
}
if args.count >= 3 && args[1] == "faces" {
    emit(args.dropFirst(2).map { ["path": $0, "faces": faces($0)] as [String: Any] })
} else if args.count == 4 && args[1] == "distance" {
    guard let a = featurePrint(args[2]), let b = featurePrint(args[3]) else { emit(["error": "unreadable image"]); exit(2) }
    var d: Float = 0
    do { try a.computeDistance(&d, to: b) } catch { emit(["error": "\(error)"]); exit(2) }
    emit(["distance": Double(d)])
} else {
    FileHandle.standardError.write("usage: vision_faces faces <img>... | distance <a> <b>\n".data(using: .utf8)!)
    exit(64)
}
