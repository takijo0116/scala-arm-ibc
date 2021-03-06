import os
from math import radians
from typing import List, Tuple, Optional, Sequence

import numpy as np
import tensorflow as tf
from tf_agents.environments import suite_gym, PyEnvironment
from tf_agents.trajectories import PolicyStep, trajectory, StepType
from tf_agents.utils import example_encoding_dataset
from trio_serial import SerialStream, AbstractSerialStream

from camera.image import ImageShape
from motors.core.serial_client import SerialClient
from env.arm_kinematics_2d import forward
from env.robot_arm_real_infra import RobotArmRealInfra, open_arm_control
from motors.futaba.commands import Commands as FutabaCommands


class _HumanControllerInfra:
    _MOTOR_IDS = [49, 9]  # [root, end effector]
    _SIGN_CONVERSIONS = [-1, 1]  # conversion from raw motor input to right-hand system

    def __init__(self, robot_infra: RobotArmRealInfra, serial_stream: AbstractSerialStream):
        self._robot_infra = robot_infra
        self._serial_client = SerialClient(serial_stream)
        self._commands = [FutabaCommands.read_position(id_) for id_ in self._MOTOR_IDS]

    async def read_current_xy_and_angles(self) -> Tuple[np.ndarray, List[float]]:
        angles = [sign_conv * radians((await self._serial_client.query(command)).angle)
                  for command, sign_conv in zip(self._commands, self._SIGN_CONVERSIONS)]
        return np.asarray(forward(*angles)), angles

    async def compute_action(self) -> Tuple[PolicyStep, List[float]]:
        input_xy, angles = await self.read_current_xy_and_angles()
        robot_target_pos = self._robot_infra.target_position
        xy_diff = input_xy - robot_target_pos if robot_target_pos is not None else np.zeros(2)
        return PolicyStep(action=np.asarray(xy_diff, dtype=np.float32)), angles


class OracleRecorder:
    """record oracle by using manual controller system"""

    @staticmethod
    def _generate_next_record_file_name(dataset_path: str):
        # dataset_path is like test*.tfrecord
        shards = tf.io.gfile.glob(dataset_path)
        file_count = len(shards)
        split_path = os.path.splitext(dataset_path)
        next_dataset_path = f'{split_path[0].replace("*", "")}_{file_count}{split_path[1]}'
        return next_dataset_path

    @classmethod
    def _generate_tf_observer(cls, env: PyEnvironment, dataset_path: str):
        data_spec = trajectory.from_transition(
            env.time_step_spec(), PolicyStep(env.action_spec()),
            env.time_step_spec())
        return example_encoding_dataset.TFRecordObserver(
            cls._generate_next_record_file_name(dataset_path),
            data_spec,
            py_mode=True,
            compress_image=True)

    @classmethod
    async def record(
            cls,
            env_name: str,
            dataset_path: str,
            target_update_delta_time: float,
            command_delta_time: float,
            observations: Optional[Sequence] = None,
            image_shape: Optional[ImageShape] = None,
            serial_port_name: str = '',
            controller_serial_port_name: str = ''
    ):
        last_trajectory = None
        async with SerialStream(controller_serial_port_name, baudrate=115200) as ttl_serial, \
                open_arm_control(
                    serial_port_name, target_update_delta_time, command_delta_time, observations) as robot_infra:
            try:
                controller_infra = _HumanControllerInfra(robot_infra, ttl_serial)
                current_xy = (await controller_infra.read_current_xy_and_angles())[0]
                env = suite_gym.load(env_name, gym_kwargs={
                    'delta_time': target_update_delta_time,
                    'observations': observations,
                    'image_shape': image_shape,
                    'reset_position': current_xy,
                })
                observer = cls._generate_tf_observer(env, dataset_path)

                print('resetting !!!')
                await env.async_reset(robot_infra)
                time_step = env.reset()
                print('reset end !!! start recording !!!')
                while True:
                    action_step, _ = await controller_infra.compute_action()
                    await env.async_step(robot_infra, action_step.action)
                    next_time_step = env.step(None)

                    last_trajectory = trajectory.from_transition(time_step, action_step, next_time_step)
                    observer(last_trajectory)
                    time_step = next_time_step
            finally:
                if last_trajectory is not None:
                    observer(last_trajectory.replace(step_type=StepType.LAST))
