# python demo_track.py video -expn 'test' -n 'track1' --path /home/BONDEN5-mathan-2022-03-14/videos/BONDEN5_20210616_124719.avi --outdir 'track_out' --save_result
import argparse
import os
import os.path as osp
import time
import cv2
import torch
import numpy as np

from loguru import logger

from yolox.data.data_augment import preproc
from yolox.exp import get_exp
from yolox.utils import fuse_model, get_model_info, postprocess
from yolox.utils.visualize import plot_tracking
from yolox.tracker.byte_tracker import BYTETracker
from yolox.tracking_utils.timer import Timer

import sys
sys.path.append('../../trained_detector/ScaledYOLOv4/')
from pathlib import Path

FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative

from models.experimental import attempt_load
from utils.general import (check_img_size, non_max_suppression, scale_coords)
from numpy import random
# import detect_y5 as detect
from utils.torch_utils import select_device
from utils.datasets import letterbox



IMAGE_EXT = [".jpg", ".jpeg", ".webp", ".bmp", ".png"]


def make_parser():
    parser = argparse.ArgumentParser("ByteTrack Demo!")
    parser.add_argument(
        "demo", default="image", help="demo type, eg. image, video and webcam"
    )
    parser.add_argument("-expn", "--experiment-name", type=str, default=None)
    parser.add_argument("-n", "--name", type=str, default=None, help="model name")

    parser.add_argument(
        #"--path", default="./datasets/mot/train/MOT17-05-FRCNN/img1", help="path to images or video"
        "--path", default="./videos/palace.mp4", help="path to images or video"
    )
    parser.add_argument(
        #"--path", default="./datasets/mot/train/MOT17-05-FRCNN/img1", help="path to images or video"
        "--outdir", default="./track/", help="path of output dir"
    )
    parser.add_argument("--camid", type=int, default=0, help="webcam demo camera id")
    parser.add_argument(
        "--save_result",
        action="store_true",
        help="whether to save the inference result of image/video",
    )

    # exp file
    parser.add_argument(
        "-f",
        "--exp_file",
        default=None,
        type=str,
        help="pls input your expriment description file",
    )
    parser.add_argument("-c", "--ckpt", default=None, type=str, help="ckpt for eval")
    parser.add_argument(
        "--device",
        default="gpu",
        type=str,
        help="device to run our model, can either be cpu or gpu",
    )
    parser.add_argument("--conf", default=None, type=float, help="test conf")
    parser.add_argument("--nms", default=None, type=float, help="test nms threshold")
    parser.add_argument("--tsize", default=None, type=int, help="test img size")
    parser.add_argument("--fps", default=30, type=int, help="frame rate (fps)")
    parser.add_argument(
        "--fp16",
        dest="fp16",
        default=False,
        action="store_true",
        help="Adopting mix precision evaluating.",
    )
    parser.add_argument(
        "--fuse",
        dest="fuse",
        default=False,
        action="store_true",
        help="Fuse conv and bn for testing.",
    )
    parser.add_argument(
        "--trt",
        dest="trt",
        default=False,
        action="store_true",
        help="Using TensorRT model for testing.",
    )
    # tracking args
    parser.add_argument("--track_thresh", type=float, default=0.35, help="tracking confidence threshold")
    parser.add_argument("--track_buffer", type=int, default=30, help="the frames for keep lost tracks")
    parser.add_argument("--match_thresh", type=float, default=0.8, help="matching threshold for tracking")
    parser.add_argument(
        "--aspect_ratio_thresh", type=float, default=1.6,
        help="threshold for filtering out boxes of which aspect ratio are above the given value."
    )
    parser.add_argument('--min_box_area', type=float, default=50, help='filter out tiny boxes')
    parser.add_argument("--mot20", dest="mot20", default=False, action="store_true", help="test mot20.")
    
    return parser


def get_image_list(path):
    image_names = []
    for maindir, subdir, file_name_list in os.walk(path):
        for filename in file_name_list:
            apath = osp.join(maindir, filename)
            ext = osp.splitext(apath)[1]
            if ext in IMAGE_EXT:
                image_names.append(apath)
    return image_names


def write_results(filename, results):
    save_format = '{frame},{id},{x1},{y1},{w},{h},{s},-1,-1,-1\n'
    with open(filename, 'w') as f:
        for frame_id, tlwhs, track_ids, scores in results:
            for tlwh, track_id, score in zip(tlwhs, track_ids, scores):
                if track_id < 0:
                    continue
                x1, y1, w, h = tlwh
                line = save_format.format(frame=frame_id, id=track_id, x1=round(x1, 1), y1=round(y1, 1), w=round(w, 1), h=round(h, 1), s=round(score, 2))
                f.write(line)
    logger.info('save results to {}'.format(filename))


class Detecter:
    def __init__(
        self,
        weights=ROOT / 'yolov5s.pt',  # model.pt path(s)
        # source=ROOT / 'data/images',  # file/dir/URL/glob, 0 for webcam
        data=ROOT / '../../trained_detector/yolov5/data/coco128.yaml',  # dataset.yaml path
        imgsz=(640, 640),  # inference size (height, width)
        conf_thres=0.25,  # confidence threshold
        iou_thres=0.45,  # NMS IOU threshold
        max_det=1000,  # maximum detections per image
        device='',  # cuda device, i.e. 0 or 0,1,2,3 or cpu
        classes=None,  # filter by class: --class 0, or --class 0 2 3
        agnostic_nms=False,  # class-agnostic NMS
        augment=False,  # augmented inference
        visualize=False,  # visualize features
        half=False,  # use FP16 half-precision inference
        dnn=False,  # use OpenCV DNN for ONNX inference
    ):
        self.device = select_device(device)
        self.model = attempt_load(weights, map_location=self.device)  # load FP32 model
        if half:
            self.model.half()  # to FP16
        
        # self.stride, self.names, self.pt = self.model.stride, self.model.names, self.model.pt
        self.imgsz = check_img_size(imgsz[0], s=self.model.stride.max().item())  # check image size
        
        # Get names and colors
        names = self.model.module.names if hasattr(self.model, 'module') else self.model.names
        colors = [[random.randint(0, 255) for _ in range(3)] for _ in range(len(names))]
        
        img = torch.zeros((1, 3, imgsz[0], imgsz[1]), device=self.device)  # init img
        _ = self.model(img.half() if half else img) if self.device.type != 'cpu' else None  # run once
    
        self.conf_thres, self.iou_thres, self.max_det = conf_thres, iou_thres, max_det
        self.agnostic_nms = agnostic_nms
        self.classes = classes

        self.augment = augment
        self.visualize = visualize
    
    def detect(
        self,
        im0s
    ):
        dt, seen = [0.0, 0.0, 0.0], 0
        im = letterbox(im0s, self.imgsz, auto=False)[0]
        # Convert
        im = im.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
        im = np.ascontiguousarray(im)

        im = torch.from_numpy(im).to(self.device)
        im =  im.float()  # uint8 to fp16/32
        im /= 255  # 0 - 255 to 0.0 - 1.0
        if len(im.shape) == 3:
            im = im[None]  # expand for batch dim

        # Inference
        pred = self.model(im, augment=self.augment)[0]

        # NMS
        pred = non_max_suppression(pred, self.conf_thres, self.iou_thres, classes = self.classes, agnostic=self.agnostic_nms)

        s = ''
        # Process predictions
        for i, det in enumerate(pred):  # per image
            seen += 1
            im0 = im0s.copy()
            s += '%gx%g ' % im.shape[2:]  # print string
            gn = torch.tensor(im0.shape)[[1, 0, 1, 0]]  # normalization gain whwh
            # imc = im0.copy() if save_crop else im0  # for save_crop
            imc = im0
            if len(det):
                # Rescale boxes from img_size to im0 size
                det[:, :4] = scale_coords(im.shape[2:], det[:, :4], im0.shape).round()

                # Print results
                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()  # detections per class

                # Write results
                # print(det, det.shape)
            return det, self.device
    


class Predictor(object):
    def __init__(
        self,
        model,
        exp,
        trt_file=None,
        decoder=None,
        device=torch.device("cpu"),
        fp16=False
    ):
        # self.model = model
        # self.decoder = decoder
        self.num_classes = 3
        # self.confthre = exp.test_conf
        # self.nmsthre = exp.nmsthre
        # self.test_size = exp.test_size
        self.test_size = (1280, 1280)
        # self.device = device
        self.device = str(torch.cuda.current_device())
        self.fp16 = fp16
        # if trt_file is not None:
        #     from torch2trt import TRTModule

        #     model_trt = TRTModule()
        #     model_trt.load_state_dict(torch.load(trt_file))

        #     x = torch.ones((1, 3, exp.test_size[0], exp.test_size[1]), device=device)
        #     self.model(x)
        #     self.model = model_trt
        self.rgb_means = (0.485, 0.456, 0.406)
        self.std = (0.229, 0.224, 0.225)
        self.model = Detecter(
            weights = '../../trained_detector/ScaledYOLOv4/weights/best_all_yolov4-pp6.pt',
            imgsz = (1280, 1280),
            device = self.device,
            classes = 0,
            visualize = False,
            conf_thres=0.05,  # confidence threshold
            iou_thres=0.35,  # NMS IOU threshold
            half=self.fp16,  # use FP16 half-precision inference
        )

        

    def inference(self, img, timer):
        # device = select_device(self.device)
        img_info = {"id": 0}
        if isinstance(img, str):
            img_info["file_name"] = osp.basename(img)
            img = cv2.imread(img)
        else:
            img_info["file_name"] = None

        # print(img.shape, '--img')
        height, width = img.shape[:2]
        img_info["height"] = height
        img_info["width"] = width
        img_info["raw_img"] = img

        # img, ratio = preproc(img, self.test_size, self.rgb_means, self.std)
        # img_info["ratio"] = ratio
        # img = torch.from_numpy(img).unsqueeze(0).float().to(device)
        if self.fp16:
            img = img.half()  # to FP16

        with torch.no_grad():
            timer.tic()
            # outputs = self.model(img)
            outputs, self.device = self.model.detect(im0s=img) # Img input from tracker
            # if self.decoder is not None:
            #     outputs = self.decoder(outputs, dtype=outputs.type())
            # outputs = postprocess(
            #     outputs, self.num_classes, self.confthre, self.nmsthre
            # )
            
            #logger.info("Infer time: {:.4f}s".format(time.time() - t0))
        # print(outputs.shape, '--op')
        return outputs, img_info


def image_demo(predictor, vis_folder, current_time, args):
    if osp.isdir(args.path):
        files = get_image_list(args.path)
    else:
        files = [args.path]
    files.sort()
    tracker = BYTETracker(args, frame_rate=args.fps)
    timer = Timer()
    results = []

    for frame_id, img_path in enumerate(files, 1):
        outputs, img_info = predictor.inference(img_path, timer)
        if outputs[0] is not None:
            online_targets = tracker.update(outputs[0], [img_info['height'], img_info['width']], self.test_size)
            online_tlwhs = []
            online_ids = []
            online_scores = []
            for t in online_targets:
                tlwh = t.tlwh
                tid = t.track_id
                vertical = tlwh[2] / tlwh[3] > args.aspect_ratio_thresh
                if tlwh[2] * tlwh[3] > args.min_box_area and not vertical:
                    online_tlwhs.append(tlwh)
                    online_ids.append(tid)
                    online_scores.append(t.score)
                    # save results
                    results.append(
                        f"{frame_id},{tid},{tlwh[0]:.2f},{tlwh[1]:.2f},{tlwh[2]:.2f},{tlwh[3]:.2f},{t.score:.2f},-1,-1,-1\n"
                    )
            timer.toc()
            online_im = plot_tracking(
                img_info['raw_img'], online_tlwhs, online_ids, frame_id=frame_id, fps=1. / timer.average_time, det = outputs[0]
            )
        else:
            timer.toc()
            online_im = img_info['raw_img']

        # result_image = predictor.visual(outputs[0], img_info, predictor.confthre)
        if args.save_result:
            timestamp = time.strftime("%Y_%m_%d_%H_%M_%S", current_time)
            save_folder = osp.join(vis_folder, timestamp)
            os.makedirs(save_folder, exist_ok=True)
            cv2.imwrite(osp.join(save_folder, osp.basename(img_path)), online_im)

        if frame_id % 20 == 0:
            logger.info('Processing frame {} ({:.2f} fps)'.format(frame_id, 1. / max(1e-5, timer.average_time)))

        ch = cv2.waitKey(0)
        if ch == 27 or ch == ord("q") or ch == ord("Q"):
            break

    if args.save_result:
        res_file = osp.join(vis_folder, f"{timestamp}.txt")
        with open(res_file, 'w') as f:
            f.writelines(results)
        logger.info(f"save results to {res_file}")


def imageflow_demo(predictor, vis_folder, current_time, args):
    cap = cv2.VideoCapture(args.path if args.demo == "video" else args.camid)
    width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)  # float
    height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)  # float
    fps = cap.get(cv2.CAP_PROP_FPS)
    timestamp = time.strftime("%Y_%m_%d_%H_%M_%S", current_time)
    save_folder = osp.join(vis_folder, timestamp)
    os.makedirs(save_folder, exist_ok=True)
    if args.demo == "video":
        save_path = osp.join(save_folder, args.path.split("/")[-1])
    else:
        save_path = osp.join(save_folder, "camera.mp4")
    logger.info(f"video save_path is {save_path}")
    vid_writer = cv2.VideoWriter(
        save_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (int(width), int(height))
    )
    tracker = BYTETracker(args, frame_rate=30)
    timer = Timer()
    frame_id = 0
    results = []
    test_size = (1280, 1280)
    while True:
        if frame_id % 20 == 0:
            logger.info('Processing frame {} ({:.2f} fps)'.format(frame_id, 1. / max(1e-5, timer.average_time)))
        ret_val, frame = cap.read()
        if ret_val:
            outputs, img_info = predictor.inference(frame, timer)
            # imag = frame.copy()
            # for o in outputs:
            #     x1, y1, x2, y2 = int(o[0]), int(o[1]), int(o[2]), int(o[3])
            #     imag = cv2.rectangle(imag, (x1, y1), (x2, y2), (255, 0, 0), 3)
            # cv2.imwrite(save_path+'img1.png', imag)
            # input('check image')
            if outputs is not None:
                online_targets = tracker.update(outputs[:,:5], [img_info['height'], img_info['width']], test_size)
                # for ot in online_targets:
                #     print(ot.tlbr)
                #     o = ot.tlbr
                #     x1, y1, x2, y2 = int(o[0]), int(o[1]), int(o[2]), int(o[3])
                #     imag = cv2.rectangle(imag, (x1, y1), (x2, y2), (0, 0, 255), 3)
                # cv2.imwrite(save_path+'TRACKKK.png', imag)
                online_tlwhs = []
                online_ids = []
                online_scores = []
                for t in online_targets:
                    tlwh = t.tlwh
                    tid = t.track_id
                    vertical = tlwh[2] / tlwh[3] > args.aspect_ratio_thresh
                    if tlwh[2] * tlwh[3] > args.min_box_area and not vertical:
                        online_tlwhs.append(tlwh)
                        online_ids.append(tid)
                        online_scores.append(t.score)
                        results.append(
                            f"{frame_id},{tid},{tlwh[0]:.2f},{tlwh[1]:.2f},{tlwh[2]:.2f},{tlwh[3]:.2f},{t.score:.2f},-1,-1,-1\n"
                        )
                timer.toc()
                online_im = plot_tracking(
                    img_info['raw_img'], online_tlwhs, online_ids, frame_id=frame_id + 1, fps=1. / timer.average_time, det=outputs
                )
            else:
                timer.toc()
                online_im = img_info['raw_img']
            if args.save_result:
                vid_writer.write(online_im)
            ch = cv2.waitKey(1)
            if ch == 27 or ch == ord("q") or ch == ord("Q"):
                break
        else:
            break
        frame_id += 1

    if args.save_result:
        res_file = osp.join(vis_folder, f"{timestamp}.txt")
        with open(res_file, 'w') as f:
            f.writelines(results)
        logger.info(f"save results to {res_file}")


def main(exp, args):
    if not args.experiment_name:
        # args.experiment_name = exp.exp_name
        args.experiment_name = 'track'

    output_dir = osp.join(args.outdir, args.experiment_name)
    os.makedirs(output_dir, exist_ok=True)

    if args.save_result:
        vis_folder = osp.join(output_dir, "track_vis")
        os.makedirs(vis_folder, exist_ok=True)

    if args.trt:
        args.device = "gpu"
    args.device = torch.device("cuda" if args.device == "gpu" else "cpu")

    logger.info("Args: {}".format(args))

    # if args.conf is not None:
    #     exp.test_conf = args.conf
    # if args.nms is not None:
    #     exp.nmsthre = args.nms
    # if args.tsize is not None:
    #     exp.test_size = (args.tsize, args.tsize)

    # model = exp.get_model().to(args.device)
    # logger.info("Model Summary: {}".format(get_model_info(model, exp.test_size)))
    # model.eval()

    # if not args.trt:
    #     if args.ckpt is None:
    #         ckpt_file = osp.join(output_dir, "best_ckpt.pth.tar")
    #     else:
    #         ckpt_file = args.ckpt
    #     logger.info("loading checkpoint")
    #     ckpt = torch.load(ckpt_file, map_location="cpu")
    #     # load the model state dict
    #     model.load_state_dict(ckpt["model"])
    #     logger.info("loaded checkpoint done.")

    # if args.fuse:
    #     logger.info("\tFusing model...")
    #     model = fuse_model(model)

    # if args.fp16:
    #     model = model.half()  # to FP16

    # if args.trt:
    #     assert not args.fuse, "TensorRT model is not support model fusing!"
    #     trt_file = osp.join(output_dir, "model_trt.pth")
    #     assert osp.exists(
    #         trt_file
    #     ), "TensorRT model is not found!\n Run python3 tools/trt.py first!"
    #     model.head.decode_in_inference = False
    #     decoder = model.head.decode_outputs
    #     logger.info("Using TensorRT to inference")
    # else:
    #     trt_file = None
    #     decoder = None

    model = None
    trt_file = None
    decoder = None
    predictor = Predictor(model, exp, trt_file, decoder, args.device, args.fp16)
    current_time = time.localtime()
    if args.demo == "image":
        image_demo(predictor, vis_folder, current_time, args)
    elif args.demo == "video" or args.demo == "webcam":
        imageflow_demo(predictor, vis_folder, current_time, args)


if __name__ == "__main__":
    args = make_parser().parse_args()
    # exp = get_exp(args.exp_file, args.name)
    exp = None
    main(exp, args)
