import trio
from tf_agents.environments import suite_gym, HistoryWrapper
from tf_agents.policies import SavedModelPyTFEagerPolicy
from trio_serial import SerialStream

from camera.video_capture import open_video_capture
from controller.oracle_recorder import OracleRecorder, IMAGE_SIZE
from env.robot_arm_real_infra import RobotArmRealInfra


class PolicyExecutor:
    _HISTORY_LENGTH = 2

    def __init__(self, saved_model_path: str, checkpoint_path: str):
        policy = SavedModelPyTFEagerPolicy(
            saved_model_path, load_specs_from_pbtxt=True)
        policy.update_from_checkpoint(checkpoint_path)
        self._policy = policy

    async def run(self):
        async with SerialStream('/dev/ttyUSB0', baudrate=115200) as rs485_serial:
            with open_video_capture(0) as cap:
                robot_infra = RobotArmRealInfra(rs485_serial, cap, IMAGE_SIZE)
                env = suite_gym.load('ScalaArm-v0', gym_kwargs={
                    'infra': robot_infra,
                    'delta_time': OracleRecorder.DELTA_TIME,
                    'image_size': IMAGE_SIZE,
                })
                env = HistoryWrapper(
                    env, history_length=self._HISTORY_LENGTH, tile_first_step_obs=True)
                await env.async_reset()
                time_step = env.reset()
                while True:
                    start_at = trio.current_time()
                    action_step = self._policy.action(time_step)
                    # print('estimate end', trio.current_time() - start_at)
                    action = action_step.action
                    # print(action)
                    await env.async_step(action)
                    time_step = env.step(None)
                    # print('one cycle end: ', trio.current_time() - start_at)