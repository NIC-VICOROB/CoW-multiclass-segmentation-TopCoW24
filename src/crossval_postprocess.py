import os
import json
import torch
import argparse
import numpy as np
import SimpleITK as sitk

from tqdm import tqdm
from natsort import natsorted
from scipy.ndimage import label
from nnUNet.nnunetv2.inference.predict_from_raw_data import nnUNetPredictor
from nnUNet.nnunetv2.imageio.simpleitk_reader_writer import SimpleITKIO
from post_processings import *


''' 
cmd:
export PYTHONPATH=/home/hasna/miccai24_challenges/topcow_challenge_final
export nnUNet_raw='/home/hasna/miccai24_challenges/topcow_challenge_final/nnunet_dir/dataset/nnUNet_raw'
export nnUNet_preprocessed='/home/hasna/miccai24_challenges/topcow_challenge_final/nnunet_dir/dataset/preprocessed'
export nnUNet_results='/home/hasna/miccai24_challenges/topcow_challenge_final/nnunet_dir/datasetnnUNet_trained_models'


>>> Final inferences:
    > MRA: python /home/hasna/miccai24_challenges/topcow_challenge/src/crossval_postprocess.py --model_bin_path /home/hasna/miccai24_challenges/topcow_challenge/nnunet_dir/datasetnnUNet_trained_models/Dataset802_TopCoWSegBinMRA/nnUNetTrainerSkeletonRecall__nnUNetPlans__3d_fullres --model_mul_path /home/hasna/miccai24_challenges/topcow_challenge/nnunet_dir/datasetnnUNet_trained_models/Dataset808_TopCoWSegMRA/nnUNetTrainerSkeletonRecallBinDiceNoMirroring__nnUNetPlans__3d_fullres_pretrained --split_path /home/hasna/miccai24_challenges/topcow_challenge/nnunet_dir/dataset/preprocessed/Dataset808_TopCoWSegMRA/splits_final.json --input_images /home/hasna/miccai24_challenges/topcow_challenge/nnunet_dir/dataset/nnUNet_raw/Dataset808_TopCoWSegMRA/imagesTr --save_folder /home/hasna/miccai24_challenges/topcow_challenge/evals/final_MRA_bin802_mul808_skr-bindice_nomir_5folds_val-best --num_folds 5 --mod 'mr' --gpu 1
    > CTA: python /home/hasna/miccai24_challenges/topcow_challenge/src/crossval_postprocess.py --model_bin_path /home/hasna/miccai24_challenges/topcow_challenge/nnunet_dir/datasetnnUNet_trained_models/Dataset809_TopCoWSegBinCTA/nnUNetTrainerSkeletonRecall__nnUNetPlans__3d_fullres_1000epochs --model_mul_path /home/hasna/miccai24_challenges/topcow_challenge/nnunet_dir/datasetnnUNet_trained_models/Dataset806_TopCoWSegCTAMRA/nnUNetTrainerSkeletonRecallBinDiceNoMirroring__nnUNetPlans__3d_fullres_ps --split_path /home/hasna/miccai24_challenges/topcow_challenge/nnunet_dir/dataset/preprocessed/Dataset809_TopCoWSegBinCTA/splits_final.json --input_images /home/hasna/miccai24_challenges/topcow_challenge/nnunet_dir/dataset/nnUNet_raw/Dataset809_TopCoWSegBinCTA/imagesTr --save_folder /home/hasna/miccai24_challenges/topcow_challenge/evals/final_CTA_bin809_mul806_skr-bindice_nomir_5folds_val-best --num_folds 5 --mod 'ct' --gpu 1
'''

''' --------------------------------------------------------------- '''
parser = argparse.ArgumentParser(description='Get all command line arguments.')
parser.add_argument('--model_bin_path', type=str, required=True, help='Specify the path to the binary predictions')
parser.add_argument('--model_mul_path', type=str, required=True, help='Specify the path to the multiclass model')
parser.add_argument('--split_path', type=str, required=True, help='Specify the path to the splits json file')
parser.add_argument('--input_images', type=str, default='', help='Specify the dir to al the input images')
# parser.add_argument('--preds_folder', type=str, default='', help='Specify the dir to al the preds per fold')
parser.add_argument('--save_folder', type=str, default='', help='Specify the dir to al the trained models')
parser.add_argument('--min_dist', type=float, default=15, help='Specify the minimum distance to link')
parser.add_argument('--min_vol', type=float, default=20, help='Specify the minimum volume to remove')
parser.add_argument('--num_folds', type=int, default=5, help='Specify the number of folds to be used')
parser.add_argument('--mod', type=str, default='', help='Specify the modality to be used')
parser.add_argument('--gpu', type=int, default=0, help='Specify the gpu to be used')
''' --------------------------------------------------------------- '''



def write_array_as_image_file(array, input_path, output_path):
    input_img = sitk.ReadImage(input_path)

    ## Reorder array from (x,y,z) to (z,y,x) before using sitk.GetImageFromArray
    array = array.transpose((2, 1, 0)).astype(np.uint8)

    ## Converting prediction array back to SimpleITK copying the metadata from the original image
    seg_mask = sitk.GetImageFromArray(array.astype(np.uint8))

    ## Copies the Origin, Spacing, and Direction from the source image
    seg_mask.CopyInformation(input_img)

    sitk.WriteImage(
        seg_mask,
        output_path,
        useCompression=True,
    )



def main(args):
    os.environ['CUDA_VISIBLE_DEVICES'] = str(args.gpu)

    ### Define the multiclass predictor and initialize its weights
    model_bin_path = args.model_bin_path
    model_mul_path = args.model_mul_path

    with open(args.split_path, 'r') as file:
        splits = json.load(file)

    predictor_bin = nnUNetPredictor(
        tile_step_size=0.5,
        use_gaussian=True,
        use_mirroring=True,
        perform_everything_on_device=True,
        device=torch.device('cuda', 0),
        verbose=False,
        verbose_preprocessing=False,
        allow_tqdm=False
    )
    predictor_bin.initialize_from_trained_model_folder(
        model_bin_path,
        use_folds=('all'),
        checkpoint_name='checkpoint_best.pth',
    )

    predictor_mul = nnUNetPredictor(
        tile_step_size=0.5,
        use_gaussian=True,
        use_mirroring=False,  #FIXME: disabling inference mirroring, as it was also trained with NoMirroring trainer option
        perform_everything_on_device=True,
        device=torch.device('cuda', 0),
        verbose=False,
        verbose_preprocessing=False,
        allow_tqdm=False
    )

    for fold in range(args.num_folds):  # For each fold, initialize the corresponding model..
        os.makedirs(os.path.join(args.save_folder, f"without_pp/fold_{fold}"), exist_ok=True)
        os.makedirs(os.path.join(args.save_folder, f"with_pp/fold_{fold}"), exist_ok=True)
        os.makedirs(os.path.join(args.save_folder, f"bin/fold_{fold}"), exist_ok=True)
        predictor_mul.initialize_from_trained_model_folder(
            model_mul_path,
            use_folds=(fold,),
            checkpoint_name='checkpoint_best.pth',
        )
        for file in tqdm(natsorted(splits[fold]['val'])):
            if file.startswith(f'topcow_{args.mod}_'):
            # if file.startswith(f'crown_mr_whole_'):
                file = file+'.nii.gz'
                input_path = os.path.join(args.input_images, f"{file.split('.')[0]}_0000.nii.gz")
                input_array, input_props = SimpleITKIO().read_images([input_path])  # Read input image with its properties

                pred_array_bin = predictor_bin.predict_single_npy_array(input_array, input_props)
                pred_array_bin = pred_array_bin.transpose((2, 1, 0)).astype(np.uint8)
                write_array_as_image_file(pred_array_bin, input_path, os.path.join(args.save_folder, f"bin/fold_{fold}", file))

                pred_array_mul = predictor_mul.predict_single_npy_array(input_array, input_props)
                pred_array_mul[pred_array_mul == 13] = 15
                pred_array_mul = pred_array_mul.transpose((2, 1, 0)).astype(np.uint8)
                
                write_array_as_image_file(pred_array_mul, input_path, os.path.join(args.save_folder, f"without_pp/fold_{fold}", file))


                ## --- Postprocessing --- :
                ## Step 1: Replace the BG voxels in multiclass pred & Delete the small disconnected volumes (<20 voxels)
                pred_array_mul = replace_background_with_nearest_label(pred_array_mul, pred_array_bin)
                cleaned_arr = clean_small_components(pred_array_mul, args.min_vol)

                ## Step 2: Connect the disconnected components (linkin parts)
                new_arr = np.zeros(cleaned_arr.shape)  # New array to store the modifications (bridges)
                for i in np.unique(cleaned_arr):  # Iterating over each class
                    binary_image = 1*(cleaned_arr==i)  # Dealing with 1 label class per time
                    _, nums = label(binary_image)

                    if nums > 1:  # If for that specific label, there are more than 1 component (thus, disconnexions)
                        dilated = link_components(binary_image, cleaned_arr)  # Link between the two closest points of the two closest components by dilating the line connecting them and multiply it by their class label  # FIXME: remove the clean_arr, it's useless
                        dilated_corrected = 1*( (dilated - 1*(cleaned_arr>0)) > 0)  # Get only the extra voxels to not interfere with what was originally there..
                        dilated_corrected = i*( (dilated_corrected - 1*(new_arr>0)) > 0)  # Get only the extra voxels to not interfere with what was previously dilated from other accumulated labels...
                        new_arr += (dilated_corrected.astype(np.uint8))
    
                corrected = new_arr*((1*(new_arr>0) - 1*(cleaned_arr>0)) > 0)  # Getting the final extra voxels (from bridges) to not interfer with the original + multiplying by the new_arr to get the classes in the binary (subtraction)
                cleaned_arr[corrected != 0] = 0  # Replace only where there is no interferance with the other labels !!
                cleaned_arr += (corrected.astype(np.uint8))  # Adding the final extra voxel to the original, after previously replacing the original with 0 where it should..

                pred_array = final_check(cleaned_arr)  # Final checking: if a certain label has disconnected components, the smallest is either deleted (if it's not connected to anything) or takes the label of its neighbour (to ensure continuity)..
                pred_array[pred_array == 13] = 15  # Last one just in case..
                write_array_as_image_file(pred_array, input_path, os.path.join(args.save_folder, f"with_pp/fold_{fold}", file))


# %%
if __name__ == "__main__":
    args = parser.parse_args()
    main(args)
