import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import numpy as np
from numpy import sin, cos, tan, pi, arcsin, arctan
from functools import lru_cache
import torch
from torch import nn


# Calculate kernels of SphereCNN 
@lru_cache(None)
def get_xy(delta_phi, delta_theta):
    return np.array([
        [
            (-tan(delta_theta), 1/cos(delta_theta)*tan(delta_phi)),
            (0, tan(delta_phi)),
            (tan(delta_theta), 1/cos(delta_theta)*tan(delta_phi)),
        ],
        [
            (-tan(delta_theta), 0),
            (1, 1),
            (tan(delta_theta), 0),
        ],
        [
            (-tan(delta_theta), -1/cos(delta_theta)*tan(delta_phi)),
            (0, -tan(delta_phi)),
            (tan(delta_theta), -1/cos(delta_theta)*tan(delta_phi)),
        ]
    ])

@lru_cache(None)
def cal_index(h, w, img_r, img_c):
    '''  
        Calculate Kernel Sampling Pattern
        only support 3x3 filter
        return 9 locations: (3, 3, 2)
    '''
    # pixel -> rad
    phi = -((img_r+0.5)/h*pi - pi/2)
    theta = (img_c+0.5)/w*2*pi-pi

    delta_phi = pi/h
    delta_theta = 2*pi/w

    xys = get_xy(delta_phi, delta_theta)
    x = xys[..., 0]
    y = xys[..., 1]
    rho = np.sqrt(x**2+y**2)
    v = arctan(rho)
    new_phi= arcsin(cos(v)*sin(phi) + y*sin(v)*cos(phi)/rho)
    new_theta = theta + arctan(x*sin(v) / (rho*cos(phi)*cos(v) - y*sin(phi)*sin(v)))
    # rad -> pixel
    new_r = (-new_phi+pi/2)*h/pi - 0.5
    new_c = (new_theta+pi)*w/2/pi - 0.5
    # indexs out of image, equirectangular leftmost and rightmost pixel is adjacent
    new_c = (new_c + w) % w
    new_result = np.stack([new_r, new_c], axis=-1)
    new_result[1, 1] = (img_r, img_c)
    return new_result

@lru_cache(None)
def gen_filters_coordinates(h, w, stride=1):
    '''
    return np array of kernel lo (2, H/stride, W/stride, 3, 3)
    '''
    assert(isinstance(h, int) and isinstance(w, int))
    co = np.array([[cal_index(h, w, i, j) for j in range(0, w, stride)] for i in range(0, h, stride)])
    return co.transpose([4, 0, 1, 2, 3])


def map_coordinates(input, coordinates, mode='bilinear', pad='wrap'):
    ''' PyTorch version of scipy.ndimage.interpolation.map_coordinates
    input: (B, C, H, W)
    coordinates: (2, ...)
    mode: sampling method, options = {'nearest', 'bilinear'}
    pad: options = {'zero', 'wrap'}
    '''
    if not torch.is_tensor(coordinates):
        coordinates = torch.FloatTensor(coordinates).to(input.device)
    elif coordinates.dtype != torch.float32:
        coordinates = coordinates.float()
    h = input.shape[2]
    w = input.shape[3]
    
    def _coordinates_pad_wrap(h, w, coordinates):
        coordinates[0] = coordinates[0] % h
        coordinates[1] = coordinates[1] % w
        return coordinates
    
    def _coordinates_pad_zero(h, w, coordinates):
        out_of_bound_h = (coordinates[0] < 0) | (coordinates[0] > (h-1))
        out_of_bound_w = (coordinates[1] < 0) | (coordinates[1] > (w-1))
        coordinates[0, out_of_bound_h] = h
        coordinates[1, out_of_bound_w] = w
        return coordinates
    
    if mode == 'nearest':
        coordinates = torch.round(coordinates).long()
        if pad == 'wrap':
            coordinates = _coordinates_pad_wrap(h, w, coordinates)
        elif pad == 'zero':
            # coordinates: 2, 3, 3
            # out_of_bound: 3, 3
            # return: B, C, H, W = 2, 3, 3, 3
            input = nn.functional.pad(input, pad=(0, 1, 0, 1), mode='constant', value=0)
            coordinates = _coordinates_pad_zero(h, w, coordinates)
        return input[..., coordinates[0], coordinates[1]]   
    elif mode == 'bilinear':
        co_floor = torch.floor(coordinates).long()
        co_ceil = torch.ceil(coordinates).long()
        d1 = (coordinates[1] - co_floor[1].detach().float())
        d2 = (coordinates[0] - co_floor[0].detach().float())
        if pad == 'wrap':
            co_floor = _coordinates_pad_wrap(h, w, co_floor)
            co_ceil = _coordinates_pad_wrap(h, w, co_ceil)
        elif pad == 'zero':
            input = nn.functional.pad(input, pad=(0, 1, 0, 1), mode='constant', value=0)
            co_floor = _coordinates_pad_zero(h, w, co_floor)
            co_ceil = _coordinates_pad_zero(h, w, co_ceil)
        f00 = input[..., co_floor[0], co_floor[1]]
        f10 = input[..., co_floor[0], co_ceil[1]]
        f01 = input[..., co_ceil[0], co_floor[1]]
        f11 = input[..., co_ceil[0], co_ceil[1]]
        fx1 = f00 + d1*(f10 - f00)
        fx2 = f01 + d1*(f11 - f01)
        return fx1 + d2*(fx2 - fx1)    


class SphereConv2D(nn.Module):
    '''  SphereConv2D
    mode: way of sampling pixel rgb values with non-integer coordinates, options={'bilinear', 'nearest'}
    Note that this layer only support 3x3 filter
    '''
    def __init__(self, in_c, out_c, stride=1, mode='bilinear'):
        super(SphereConv2D, self).__init__()
        self.mode = mode
        self.stride = stride
        self.conv = nn.Conv2d(in_c, out_c, kernel_size=3, stride=3, padding=0)
        
    def forward(self, x):
        # x: (B, C, H, W)
        coordinates = gen_filters_coordinates(x.shape[2], x.shape[3], self.stride)
        x = map_coordinates(x, coordinates, mode=self.mode)
        x = x.permute(0, 1, 2, 4, 3, 5)
        x_sz = x.size()
        x = x.contiguous().view(x_sz[0], x_sz[1], x_sz[2]*x_sz[3], x_sz[4]*x_sz[5])
        return self.conv(x)


class SphereMaxPool2D(nn.Module):
    '''  SphereMaxPool2D
    mode: way of sampling pixel rgb values with non-integer coordinates, options={'bilinear', 'nearest'}
    Note that this layer only support 3x3 filter
    '''
    def __init__(self, stride=1, mode='bilinear'):
        super(SphereMaxPool2D, self).__init__()
        self.mode = mode
        self.stride = stride
        self.pool = nn.MaxPool2d(kernel_size=3, stride=3, padding=0, dilation=1, return_indices=False, ceil_mode=False)
        
    def forward(self, x):
        # x: (B, C, H, W)
        coordinates = gen_filters_coordinates(x.shape[2], x.shape[3], self.stride)
        x = map_coordinates(x, coordinates, mode=self.mode)
        x = x.permute(0, 1, 2, 4, 3, 5)
        x_sz = x.size()
        x = x.contiguous().view(x_sz[0], x_sz[1], x_sz[2]*x_sz[3], x_sz[4]*x_sz[5])
        return self.pool(x)
        

    
if __name__ == '__main__':    
    # test cnn
    cnn = SphereConv2D(3, 5, 1)
    out = cnn(torch.randn(2, 3, 10, 10))
    print('SphereConv2D(3, 5, 1) output shape: ', out.size())
    # test pool
    # create sample image
    h, w = 100, 200
    img = np.ones([h, w, 3])
    for r in range(h):
        for c in range(w):
            img[r, c, 0] = img[r, c, 0] - r/h
            img[r, c, 1] = img[r, c, 1] - c/w
    plt.imsave('demo_original', img)
    img = img.transpose([2, 0, 1])
    img = np.expand_dims(img, 0)  # (B, C, H, W)
    # pool
    pool = SphereMaxPool2D(1)
    out = pool(torch.from_numpy(img).float())
    out = np.squeeze(out.numpy(), 0).transpose([1, 2, 0])
    plt.imsave('demo_pool_1.png', out)
    print('Save image after pooling with stride 1: demo_pool_1.png')
    # pool with tride 3
    pool = SphereMaxPool2D(3)
    out = pool(torch.from_numpy(img).float())
    out = np.squeeze(out.numpy(), 0).transpose([1, 2, 0])
    plt.imsave('demo_pool_3.png', out)
    print('Save image after pooling with stride 3: demo_pool_3.png')