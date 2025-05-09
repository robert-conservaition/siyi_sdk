"""
Python implementation of SIYI SDK
ZR10 webpage: http://en.siyi.biz/en/Gimbal%20Camera/ZR10/overview/
Author : Mohamed Abdelkader
Email: mohamedashraf123@gmail.com
Copyright 2022

"""
import socket
from siyi_message import *
from time import sleep, time
import logging
from utils import  toInt
import threading
import cameras
import struct


class SIYISDK:
    def __init__(self, server_ip="192.168.144.25", port=37260, debug=False):
        """
        Params
        --
        - server_ip [str] IP address of the camera
        - port: [int] UDP port of the camera
        """
        self._debug = debug
        if self._debug:
            d_level = logging.DEBUG
        else:
            d_level = logging.INFO
        LOG_FORMAT = ' [%(levelname)s] %(asctime)s [SIYISDK::%(funcName)s] :\t%(message)s'
        logging.basicConfig(format=LOG_FORMAT, level=d_level)
        self._logger = logging.getLogger(self.__class__.__name__)

        # Message sent to the camera
        self._out_msg = SIYIMESSAGE(debug=self._debug)
        
        # Message received from the camera
        self._in_msg = SIYIMESSAGE(debug=self._debug)        

        self._server_ip = server_ip
        self._port = port

        self._BUFF_SIZE = 1024

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rcv_wait_t = 5  # Receiving wait time
        self._socket.settimeout(self._rcv_wait_t)

        self.resetVars()

        # Stop threads flag
        self._stop = False  
        
        self._recv_thread = threading.Thread(target=self.recvLoop)

        # Connection thread
        self._last_fw_seq = 0  # used to check on connection liveness
        self._conn_loop_rate = 1  # seconds
        self._conn_thread = threading.Thread(target=self.connectionLoop, args=(self._conn_loop_rate,))

        # Gimbal info thread @ 1Hz
        self._gimbal_info_loop_rate = 1
        self._g_info_thread = threading.Thread(target=self.gimbalInfoLoop, args=(self._gimbal_info_loop_rate,))

        # Gimbal attitude thread @ 10Hz
        self._gimbal_att_loop_rate = 0.02
        self._g_att_thread = threading.Thread(target=self.gimbalAttLoop, args=(self._gimbal_att_loop_rate,))

    def resetVars(self):
        """
        Resets variables to their initial values.
        """
        self._connected = False
        self._fw_msg = FirmwareMsg()
        self._hw_msg = HardwareIDMsg()
        self._autoFocus_msg = AutoFocusMsg()
        self._manualZoom_msg = ManualZoomMsg()
        self._manualFocus_msg = ManualFocusMsg()
        self._gimbalSpeed_msg = GimbalSpeedMsg()
        self._center_msg = CenterMsg()
        self._record_msg = RecordingMsg()
        self._mountDir_msg = MountDirMsg()
        self._motionMode_msg = MotionModeMsg()
        self._funcFeedback_msg = FuncFeedbackInfoMsg()
        self._att_msg = AttitdueMsg()
        self._set_gimbal_angles_msg = SetGimbalAnglesMsg()
        self._request_data_stream_msg = RequestDataStreamMsg()
        self._request_absolute_zoom_msg = RequestAbsoluteZoomMsg()
        self._current_zoom_level_msg = CurrentZoomValueMsg()
        self._temperature_at_point_msg = TemperatureAtPointMsg()
        self._gimbal_camera_soft_restart_msg = GimbalCameraSoftRestartMsg()
        self._request_gimbal_camera_codec_specs_msg = RequestGimbalCameraCodecSpecsMsg()
        self._send_gimbal_camera_codec_specs_msg = SendGimbalCameraCodecSpecsMsg()
        self._request_gimbal_camera_image_mode_msg = RequestGimbalCameraImageModeMsg()
        self._request_absolute_zoom_msg = RequestAbsoluteZoomMsg()
        self._last_att_seq = -1

        return True

    def connect(self, maxWaitTime=3.0, maxRetries=3):
        """
        Attempts to connect to the camera with retries if needed.
        
        Params
        --
        - maxWaitTime [float]: Maximum time to wait before giving up on connection (in seconds)
        - maxRetries [int]: Number of times to retry connecting if it fails
        """
        retries = 0
        while retries < maxRetries:
            try:
                # Initialize fresh thread instances for each connection attempt
                self._recv_thread = threading.Thread(target=self.recvLoop)
                self._conn_thread = threading.Thread(target=self.connectionLoop, args=(self._conn_loop_rate,))
                self._g_info_thread = threading.Thread(target=self.gimbalInfoLoop, args=(self._gimbal_info_loop_rate,))
                self._g_att_thread = threading.Thread(target=self.gimbalAttLoop, args=(self._gimbal_att_loop_rate,))
                
                self._logger.info(f"Attempting to connect to camera, attempt {retries + 1}")
                self._recv_thread.start()
                self._conn_thread.start()
                t0 = time()

                while True:
                    if self._connected:
                        self._logger.info(f"Successfully connected to camera on attempt {retries + 1}")
                        self._g_info_thread.start()
                        self._g_att_thread.start()

                        self.requestHardwareID()
                        sleep(0.2)
                        # self.requestDataStreamAttitude(50) # Not working 12 Sept 2024!
                        # sleep(0.5)
                        self.requestCurrentZoomLevel()
                        sleep(0.2)
                        return True

                    if (time() - t0) > maxWaitTime and not self._connected:
                        self._logger.error("Failed to connect to camera, retrying...")
                        self.disconnect()
                        retries += 1
                        break

            except Exception as e:
                self._logger.error(f"Connection attempt {retries + 1} failed: {e}")
                self.disconnect()
                retries += 1

        self._logger.error(f"Failed to connect after {maxRetries} retries")
        return False

    def disconnect(self):
        """
        Gracefully stops all threads, disconnects, and cleans up resources.
        """
        self._logger.info("Stopping all threads and disconnecting")
        self._stop = True  # Signal threads to stop

        # Close the socket to unblock any recvfrom() calls
        if self._socket:
            try:
                self._socket.close()
            except Exception as e:
                self._logger.error(f"Error closing socket: {e}")

        # Wait for threads to finish, if they're still alive
        if self._recv_thread.is_alive():
            self._recv_thread.join()
        if self._conn_thread.is_alive():
            self._conn_thread.join()
        if self._g_info_thread.is_alive():
            self._g_info_thread.join()
        if self._g_att_thread.is_alive():
            self._g_att_thread.join()

        # Reset the stop flag and other variables
        self.resetVars()
        self._stop = False

    def checkConnection(self):
        """
        Checks if there is a live connection to the camera by requesting the Firmware version.
        Runs in a thread at a defined frequency.
        """
        try:
            self.requestFirmwareVersion()
            sleep(0.1)
            if self._fw_msg.seq != self._last_fw_seq and len(self._fw_msg.gimbal_firmware_ver) > 0:
                self._connected = True
                self._last_fw_seq = self._fw_msg.seq
            else:
                self._connected = False
        except Exception as e:
            self._logger.error(f"Connection check failed: {e}")
            self.disconnect()

    def connectionLoop(self, t):
        """
        Periodically checks connection status and resets state if disconnected.

        Params
        --
        - t [float]: message frequency in seconds
        """
        while not self._stop:
            try:
                self.checkConnection()
                sleep(t)
            except Exception as e:
                self._logger.error(f"Error in connection loop: {e}")
                self.disconnect()
                break

    # def recvLoop(self):
    #     """
    #     Continuously receives data from the camera.
    #     """
    #     try:
    #         while not self._stop:
    #             try:
    #                 data, addr = self._socket.recvfrom(self._BUFF_SIZE)
    #                 if self._stop:
    #                     break
    #                 # Process the received data here...
    #             except socket.timeout:
    #                 # If stopping, exit without logging
    #                 if self._stop:
    #                     break
    #                 # Otherwise, continue waiting
    #                 continue
    #             except OSError:
    #                 # Socket closed, exit loop
    #                 break
    #     except Exception as e:
    #         self._logger.error(f"Error in receive loop: {e}")
    #     finally:
    #         self._logger.info("Exiting recvLoop")

    def isConnected(self):
        return self._connected

    def gimbalInfoLoop(self, t):
        """
        Periodically requests gimbal info.

        Params
        --
        - t [float]: message frequency in seconds
        """
        while not self._stop:
            try:
                self.requestGimbalInfo()
                sleep(t)
            except Exception as e:
                self._logger.error(f"Error in gimbal info loop: {e}")
                self.disconnect()

    def gimbalAttLoop(self, t):
        """
        Periodically requests gimbal attitude.

        Params
        --
        - t [float]: message frequency in seconds
        """
        while not self._stop:
            try:
                self.requestGimbalAttitude()
                sleep(t)
            except Exception as e:
                self._logger.error(f"Error in gimbal attitude loop: {e}")
                self.disconnect()

    def sendMsg(self, msg):
        """
        Sends a message to the camera

        Params
        --
        msg [str] Message to send
        """
        b = bytes.fromhex(msg)
        try:
            self._socket.sendto(b, (self._server_ip, self._port))
            return True
        except Exception as e:
            self._logger.error("Could not send bytes")
            return False

    def rcvMsg(self):
        data=None
        try:
            data,addr = self._socket.recvfrom(self._BUFF_SIZE)
        except Exception as e:
            self._logger.warning("%s. Did not receive message within %s second(s)", e, self._rcv_wait_t)
        return data

    def recvLoop(self):
        self._logger.debug("Started data receiving thread")
        while( not self._stop):
            self.bufferCallback()
        self._logger.debug("Exiting data receiving thread")

    
    def bufferCallback(self):
        """
        Receives messages and parses its content
        """
        try:
            buff,addr = self._socket.recvfrom(self._BUFF_SIZE)
        except Exception as e:
            self._logger.error(f"[bufferCallback] {e}")
            return

        buff_str = buff.hex()
        self._logger.debug("Buffer: %s", buff_str)

        # 10 bytes: STX+CTRL+Data_len+SEQ+CMD_ID+CRC16
        #            2 + 1  +    2   + 2 +   1  + 2
        MINIMUM_DATA_LENGTH=10*2

        HEADER='5566'
        # Go through the buffer
        while(len(buff_str)>=MINIMUM_DATA_LENGTH):
            if buff_str[0:4]!=HEADER:
                # Remove the 1st element and continue 
                tmp=buff_str[1:]
                buff_str=tmp
                continue

            # Now we got minimum amount of data. Check if we have enough
            # Data length, bytes are reversed, according to SIYI SDK
            low_b = buff_str[6:8] # low byte
            high_b = buff_str[8:10] # high byte
            data_len = high_b+low_b
            data_len = int('0x'+data_len, base=16)
            char_len = data_len*2

            # Check if there is enough data (including payload)
            if(len(buff_str) < (MINIMUM_DATA_LENGTH+char_len)):
                # No useful data
                buff_str=''
                break
            
            packet = buff_str[0:MINIMUM_DATA_LENGTH+char_len]
            buff_str = buff_str[MINIMUM_DATA_LENGTH+char_len:]

            # Finally decode the packet!
            val = self._in_msg.decodeMsg(packet)
            if val is None:
                continue
            
            data, data_len, cmd_id, seq = val[0], val[1], val[2], val[3]

            if cmd_id==COMMAND.ACQUIRE_FW_VER:
                #print("-Firmware version")
                self.parseFirmwareMsg(data, seq)
            elif cmd_id==COMMAND.ACQUIRE_HW_ID:
                #print("-Hardware ID")
                self.parseHardwareIDMsg(data, seq)
            elif cmd_id==COMMAND.ACQUIRE_GIMBAL_INFO:
                #print("-Gimbal info")
                self.parseGimbalInfoMsg(data, seq)
            elif cmd_id==COMMAND.ACQUIRE_GIMBAL_ATT:
                #print("Gimbal attitude")
                self.parseAttitudeMsg(data, seq)
            elif cmd_id==COMMAND.FUNC_FEEDBACK_INFO:
                #print("-Function feedback")
                self.parseFunctionFeedbackMsg(data, seq)
            elif cmd_id==COMMAND.GIMBAL_SPEED:
                #print("-Gimbal speed")
                self.parseGimbalSpeedMsg(data, seq)
            elif cmd_id==COMMAND.AUTO_FOCUS:
                #print("-Auto focus")
                self.parseAutoFocusMsg(data, seq)
            elif cmd_id==COMMAND.MANUAL_FOCUS:
                #print("-Manual focus")
                self.parseManualFocusMsg(data, seq)
            elif cmd_id==COMMAND.MANUAL_ZOOM:
                #print("-Manual zoom")
                self.parseZoomMsg(data, seq)
            elif cmd_id==COMMAND.CENTER:
                #print("-Center")
                self.parseGimbalCenterMsg(data, seq)
            elif cmd_id==COMMAND.SET_GIMBAL_ATTITUDE:
                #print("-Set gimbal attitude")
                self.parseSetGimbalAnglesMsg(data, seq)
            elif cmd_id==COMMAND.SET_DATA_STREAM:
                #print("-Set data stream")
                self.parseRequestStreamMsg()
            elif cmd_id==COMMAND.CURRENT_ZOOM_VALUE:
                #print("-Current zoom value")
                self.parseCurrentZoomLevelMsg(data, seq)
            elif cmd_id==COMMAND.REQUEST_TEMPERATURE_AT_POINT:
                #print("-Request temperature at point")
                self.parseTemperatureAtPointMsg(data, seq)
            elif cmd_id==COMMAND.GIMBAL_CAMERA_SOFT_RESTART:
                #print("-Gimbal camera soft restart")
                self.parseGimbalCameraSoftRestartMsg(data, seq)
            elif cmd_id==COMMAND.REQUEST_GIMBAL_CAMERA_CODEC_SPECS:
                #print("-Request gimbal camera codec specs")
                self.parseRequestGimbalCameraCodecSpecsMsg(data, seq)
            elif cmd_id==COMMAND.SEND_CODEC_SPECS_TO_GIMBAL_CAMERA:
                #print("-Send codec specs to gimbal camera")
                self.parseSendGimbalCameraCodecSpecsMsg(data, seq)
            elif cmd_id==COMMAND.REQUEST_GIMBAL_CAMERA_IMAGE_MODE:
                #print("-Request gimbal camera image mode")
                self.parseRequestGimbalCameraImageModeMsg(data, seq)
                #print("-Send gimbal camera image mode")
                self.parseSendGimbalCameraImageModeMsg(data, seq)
            elif cmd_id==COMMAND.ABSOLUTE_ZOOM:
                #print("-Absolute zoom")
                self.parseRequestAbsoluteZoomMsg(data, seq)
            else:
                self._logger.warning("CMD ID is not recognized")
        
        return
    
    ##################################################
    #               Request functions                #
    ##################################################    
    def requestFirmwareVersion(self):
        """
        Sends request for firmware version

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.firmwareVerMsg()
        if not self.sendMsg(msg):
            return False
        return True

    def requestHardwareID(self):
        """
        Sends request for Hardware ID

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.hwIdMsg()
        if not self.sendMsg(msg):
            return False
        return True

    def requestGimbalAttitude(self):
        """
        Sends request for gimbal attitude

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.gimbalAttMsg()
        if not self.sendMsg(msg):
            return False
        return True

    def requestGimbalInfo(self):
        """
        Sends request for gimbal information

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.gimbalInfoMsg()
        if not self.sendMsg(msg):
            return False
        return True

    def requestFunctionFeedback(self):
        """
        Sends request for function feedback msg

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.funcFeedbackMsg()
        if not self.sendMsg(msg):
            return False
        return True

    def requestAutoFocus(self):
        """
        Sends request for auto focus

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.autoFocusMsg()
        if not self.sendMsg(msg):
            return False
        return True

    def requestZoomIn(self):
        """
        Sends request for zoom in

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.zoomInMsg()
        if not self.sendMsg(msg):
            return False
        return True

    def requestZoomOut(self):
        """
        Sends request for zoom out

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.zoomOutMsg()
        if not self.sendMsg(msg):
            return False
        return True

    def requestZoomHold(self):
        """
        Sends request for stopping zoom

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.stopZoomMsg()
        
        return self.sendMsg(msg)
    
    def requestAbsoluteZoom(self, level: float):
        msg = self._out_msg.absoluteZoomMsg(level)
        return self.sendMsg(msg)
    
    def requestCurrentZoomLevel(self):
        msg = self._out_msg.requestCurrentZoomMsg()
        return self.sendMsg(msg)
    
    def requestTemperatureAtPoint(self, x: int, y: int, get_temp_flag: int):
        msg = self._out_msg.requestTemperatureAtPointMsg(x, y, get_temp_flag)
        return self.sendMsg(msg)
    
    def requestGimbalCameraSoftRestart(self, camera_reboot: int, gimbal_reboot: int):
        msg = self._out_msg.gimbalCameraSoftRestartMsg(camera_reboot, gimbal_reboot)
        #print("Gimbal camera soft restart msg: %s", msg)
        return self.sendMsg(msg)
    
    def requestGimbalCameraCodecSpecs(self, stream_type: int):
        msg = self._out_msg.requestGimbalCameraCodecSpecsMsg(stream_type)
        #print("Request gimbal camera codec specs msg: %s", msg)
        return self.sendMsg(msg)
    
    def sendGimbalCameraCodecSpecs(self, stream_type: int, video_enc_type: int, resolution_l: int, resolution_h: int, video_bitrate: int):
        msg = self._out_msg.sendGimbalCameraCodecSpecsMsg(stream_type, video_enc_type, resolution_l, resolution_h, video_bitrate)
        return self.sendMsg(msg)
    
    def requestGimbalCameraImageMode(self):
        if self._hw_msg.cam_type_str == 'ZT6' or self._hw_msg.cam_type_str == 'ZT30':
            msg = self._out_msg.requestGimbalCameraImageModeMsg()
            return self.sendMsg(msg)
        else:
            self._logger.warning("Camera not supported for ImageMode.")
            return False
    def sendGimbalCameraImageMode(self, vdisp_mode: int):
        msg = self._out_msg.sendGimbalCameraImageModeMsg(vdisp_mode)
        return self.sendMsg(msg)

    def requestLongFocus(self):
        """
        Sends request for manual focus, long shot

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.longFocusMsg()
        
        return self.sendMsg(msg)

    def requestCloseFocus(self):
        """
        Sends request for manual focus, close shot

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.closeFocusMsg()

        return self.sendMsg(msg)

    def requestFocusHold(self):
        """
        Sends request for manual focus, stop

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.stopFocusMsg()

        return self.sendMsg(msg)

    def requestCenterGimbal(self):
        """
        Sends request for gimbal centering

        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.centerMsg()

        return self.sendMsg(msg)

    def requestGimbalSpeed(self, yaw_speed:int, pitch_speed:int):
        """
        Sends request for gimbal centering

        Params
        --
        yaw_speed [int] -100~0~100. away from zero -> fast, close to zero -> slow. Sign is for direction
        pitch_speed [int] Same as yaw_speed
        
        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.gimbalSpeedMsg(yaw_speed, pitch_speed)

        return self.sendMsg(msg)

    def requestPhoto(self):
        """
        Sends request for taking photo
        
        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.takePhotoMsg()

        return self.sendMsg(msg)

    def requestRecording(self):
        """
        Sends request for toglling video recording
        
        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.recordMsg()

        return self.sendMsg(msg)

    def requestFPVMode(self):
        """
        Sends request for setting FPV mode
        
        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.fpvModeMsg()

        return self.sendMsg(msg)

    def requestLockMode(self):
        """
        Sends request for setting Lock mode
        
        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.lockModeMsg()

        return self.sendMsg(msg)

    def requestFollowMode(self):
        """
        Sends request for setting Follow mode
        
        Returns
        --
        [bool] True: success. False: fail
        """
        msg = self._out_msg.followModeMsg()

        return self.sendMsg(msg)
    
    def requestSetAngles(self, yaw_deg:float, pitch_deg:float):
        """
        Sends request to set gimbal angles

        Returns
        --
        [bool] True: success. False: fail
        """
        if self._hw_msg.cam_type_str == '':
            self._logger.error(f"Gimbal type is not yet retrieved. Check connection.")
            return False
        
        if self._hw_msg.cam_type_str == 'A8 mini':
            if yaw_deg > cameras.A8MINI.MAX_YAW_DEG:
                self._logger.warning(f"yaw_deg {yaw_deg} exceeds max {cameras.A8MINI.MAX_YAW_DEG}. Setting it to max")
                yaw_deg = cameras.A8MINI.MAX_YAW_DEG
            if yaw_deg < cameras.A8MINI.MIN_YAW_DEG:
                self._logger.warning(f"yaw_deg {yaw_deg} exceeds min {cameras.A8MINI.MIN_YAW_DEG}. Setting it to min")
                yaw_deg = cameras.A8MINI.MIN_YAW_DEG
            if pitch_deg > cameras.A8MINI.MAX_PITCH_DEG:
                self._logger.warning(f"pitch_deg {pitch_deg} exceeds max {cameras.A8MINI.MAX_PITCH_DEG}. Setting it to max")
                pitch_deg = cameras.A8MINI.MAX_PITCH_DEG
            if pitch_deg < cameras.A8MINI.MIN_PITCH_DEG:
                self._logger.warning(f"pitch_deg {pitch_deg} exceeds min {cameras.A8MINI.MIN_PITCH_DEG}. Setting it to min")
                pitch_deg = cameras.A8MINI.MIN_PITCH_DEG

        elif self._hw_msg.cam_type_str == 'ZR10':
            if yaw_deg > cameras.ZR10.MAX_YAW_DEG:
                self._logger.warning(f"yaw_deg {yaw_deg} exceeds max {cameras.ZR10.MAX_YAW_DEG}. Setting it to max")
                yaw_deg = cameras.ZR10.MAX_YAW_DEG
            if yaw_deg < cameras.ZR10.MIN_YAW_DEG:
                self._logger.warning(f"yaw_deg {yaw_deg} exceeds min {cameras.ZR10.MIN_YAW_DEG}. Setting it to min")
                yaw_deg = cameras.ZR10.MIN_YAW_DEG
            if pitch_deg > cameras.ZR10.MAX_PITCH_DEG:
                self._logger.warning(f"pitch_deg {pitch_deg} exceeds max {cameras.ZR10.MAX_PITCH_DEG}. Setting it to max")
                pitch_deg = cameras.ZR10.MAX_PITCH_DEG
            if pitch_deg < cameras.ZR10.MIN_PITCH_DEG:
                self._logger.warning(f"pitch_deg {pitch_deg} exceeds min {cameras.ZR10.MIN_PITCH_DEG}. Setting it to min")
                pitch_deg = cameras.ZR10.MIN_PITCH_DEG

        elif self._hw_msg.cam_type_str == 'ZT6':
            if yaw_deg > cameras.ZT6.MAX_YAW_DEG:
                self._logger.warning(f"yaw_deg {yaw_deg} exceeds max {cameras.ZT6.MAX_YAW_DEG}. Setting it to max")
                yaw_deg = cameras.ZT6.MAX_YAW_DEG
            if yaw_deg < cameras.ZT6.MIN_YAW_DEG:
                self._logger.warning(f"yaw_deg {yaw_deg} exceeds min {cameras.ZT6.MIN_YAW_DEG}. Setting it to min")
                yaw_deg = cameras.ZT6.MIN_YAW_DEG
            if pitch_deg > cameras.ZT6.MAX_PITCH_DEG:
                self._logger.warning(f"pitch_deg {pitch_deg} exceeds max {cameras.ZT6.MAX_PITCH_DEG}. Setting it to max")
                pitch_deg = cameras.ZT6.MAX_PITCH_DEG
            if pitch_deg < cameras.ZT6.MIN_PITCH_DEG:
                self._logger.warning(f"pitch_deg {pitch_deg} exceeds min {cameras.ZR10.MIN_PITCH_DEG}. Setting it to min")
                pitch_deg = cameras.A8MINI.MIN_PITCH_DEG
        else:
            self._logger.warning(f"Camera not supported. Setting angles to zero")
            return False

        msg = self._out_msg.setGimbalAttitude(int(yaw_deg*10), int(pitch_deg*10))

        return self.sendMsg(msg)
    
    def requestDataStreamAttitude(self, freq: int):
        """
        Send request to send attitude stream at specific frequency

        Params
        ---
        freq: [uint_8] frequency in Hz (0, 2, 4, 5, 10, 20, 50, 100)
        """
        msg = self._out_msg.dataStreamMsg(1, freq)
        return self.sendMsg(msg)
    
    def requestDataStreamLaser(self, freq: int):
        """
        Send request to send laser stream at specific frequency

        Params
        ---
        freq: [uint_8] frequency in Hz (0, 2, 4, 5, 10, 20, 50, 100)
        """
        msg = self._out_msg.dataStreamMsg(2, freq)
        return self.sendMsg(msg)

    ####################################################
    #                Parsing functions                 #
    ####################################################
    def parseFirmwareMsg(self, msg:str, seq:int):
        try:
            self._fw_msg.gimbal_firmware_ver= msg[8:16]
            self._fw_msg.seq=seq
            
            self._logger.debug("Firmware version: %s", self._fw_msg.gimbal_firmware_ver)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseHardwareIDMsg(self, msg:str, seq:int):
        try:
            
            self._hw_msg.seq=seq
            self._hw_msg.id = msg

            in_ascii = bytes.fromhex(msg).decode('ascii')[:10]
            self._hw_msg.id = in_ascii
            
            # first two characters define the camera ID
            cam_id = in_ascii[:2]
            try:
                self._hw_msg.cam_type_str = self._hw_msg.CAM_DICT[cam_id]
            except Exception as e:
                self._logger.error(f"Camera not recognized. Key: {cam_id}")
                self._logger.error("Camera not recognized Error %s", e)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseAttitudeMsg(self, msg:str, seq:int):
        
        try:
            self._att_msg.seq=seq
            self._att_msg.yaw = toInt(msg[2:4]+msg[0:2]) /10.
            self._att_msg.pitch = toInt(msg[6:8]+msg[4:6]) /10.
            self._att_msg.roll = toInt(msg[10:12]+msg[8:10]) /10.
            self._att_msg.yaw_speed = toInt(msg[14:16]+msg[12:14]) /10.
            self._att_msg.pitch_speed = toInt(msg[18:20]+msg[16:18]) /10.
            self._att_msg.roll_speed = toInt(msg[22:24]+msg[20:22]) /10.

            self._logger.debug("(yaw, pitch, roll= (%s, %s, %s)", 
                                    self._att_msg.yaw, self._att_msg.pitch, self._att_msg.roll)
            self._logger.debug("(yaw_speed, pitch_speed, roll_speed= (%s, %s, %s)", 
                                    self._att_msg.yaw_speed, self._att_msg.pitch_speed, self._att_msg.roll_speed)
            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseGimbalInfoMsg(self, msg:str, seq:int):
        try:
            self._record_msg.seq=seq
            self._mountDir_msg.seq=seq
            self._motionMode_msg.seq=seq
            
            self._record_msg.state = int('0x'+msg[6:8], base=16)
            self._motionMode_msg.mode = int('0x'+msg[8:10], base=16)
            self._mountDir_msg.dir = int('0x'+msg[10:12], base=16)

            self._logger.debug("Recording state %s", self._record_msg.state)
            self._logger.debug("Mounting direction %s", self._mountDir_msg.dir)
            self._logger.debug("Gimbal motion mode %s", self._motionMode_msg.mode)
            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False
    def parseRequestAbsoluteZoomMsg(self, msg:str, seq:int):
        try:
            self._request_absolute_zoom_msg.seq = seq
            self._request_absolute_zoom_msg.success = bool(int('0x'+msg, base=16))
            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseAutoFocusMsg(self, msg:str, seq:int):
        
        try:
            self._autoFocus_msg.seq=seq
            self._autoFocus_msg.success = bool(int('0x'+msg, base=16))

            
            self._logger.debug("Auto focus success: %s", self._autoFocus_msg.success)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseZoomMsg(self, msg:str, seq:int):
        
        try:
            self._manualZoom_msg.seq=seq
            self._manualZoom_msg.level = int('0x'+msg[2:4]+msg[0:2], base=16) /10.

            
            self._logger.debug("Zoom level %s", self._manualZoom_msg.level)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseManualFocusMsg(self, msg:str, seq:int):
        
        try:
            self._manualFocus_msg.seq=seq
            self._manualFocus_msg.success = bool(int('0x'+msg, base=16))

            
            self._logger.debug("Manual  focus success: %s", self._manualFocus_msg.success)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseGimbalSpeedMsg(self, msg:str, seq:int):
        
        try:
            self._gimbalSpeed_msg.seq=seq
            self._gimbalSpeed_msg.success = bool(int('0x'+msg, base=16))

            
            self._logger.debug("Gimbal speed success: %s", self._gimbalSpeed_msg.success)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseGimbalCenterMsg(self, msg:str, seq:int):
        
        try:
            self._center_msg.seq=seq
            self._center_msg.success = bool(int('0x'+msg, base=16))

            
            self._logger.debug("Gimbal center success: %s", self._center_msg.success)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseFunctionFeedbackMsg(self, msg:str, seq:int):
        
        try:
            self._funcFeedback_msg.seq=seq
            self._funcFeedback_msg.info_type = int('0x'+msg, base=16)

            
            self._logger.debug("Function Feedback Code: %s", self._funcFeedback_msg.info_type)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False
        
    def parseSetGimbalAnglesMsg(self, msg:str, seq:int):
        
        try:
            self._set_gimbal_angles_msg.seq=seq

            # No need to parse the feedback angle as it is done by the parseAttitudeMsg() in a loop

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False
        
    def parseRequestStreamMsg(self, msg:str, seq:int):
        
        try:
            self._request_data_stream_msg.seq=seq

            self._request_data_stream_msg.data_type = int('0x'+msg, base=16)

            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False
        
    def parseCurrentZoomLevelMsg(self, msg: str, seq: int):
        try:
            self._current_zoom_level_msg.seq = seq
            int_part = int('0x'+msg[0:2], base=16)
            float_part = int('0x'+msg[2:4], base=16)
            self._current_zoom_level_msg.level = int_part + (float_part/10)
            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False
    
    def parseTemperatureAtPointMsg(self, msg:str, seq:int):
        try:
            self._temperature_at_point_msg.seq=seq
            self._temperature_at_point_msg.temp = int('0x'+msg[2:4]+msg[0:2], base=16)/100.
            self._temperature_at_point_msg.x = int('0x'+msg[6:8]+msg[4:6], base=16)
            self._temperature_at_point_msg.y = int('0x'+msg[10:12]+msg[8:10], base=16)
            #print(f"Temperature at point msg: {self._temperature_at_point_msg.temp}, {self._temperature_at_point_msg.x}, {self._temperature_at_point_msg.y}")
            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False

    def parseGimbalCameraSoftRestartMsg(self, msg:str, seq:int):
        """
        Parse the gimbal camera soft restart message
        camera_reboot_status: 0: No action, 1: Camera restart; uint8_t
        gimbal_reboot_status: 0: No action, 1: Gimbal restart; uint8_t
        """
        try:
            self._gimbal_camera_soft_restart_msg.seq=seq
            self._gimbal_camera_soft_restart_msg.camera_reboot_status = int('0x'+msg[0:2], base=16)
            self._gimbal_camera_soft_restart_msg.gimbal_reboot_status = int('0x'+msg[2:4], base=16)
            return True
        except Exception as e:
            self._logger.error("Error %s", e)
            return False
    
    def parseRequestGimbalCameraCodecSpecsMsg(self, msg:str, seq:int):
        """
        Parse the gimbal camera codec specs request message
        msg: hex string containing stream type
        seq: sequence number
        Returns True if parsing successful, False otherwise
        """
        try:
            data_bytes = bytes.fromhex(msg)
            stream_type, video_enc_type, res_l, res_h, bitrate, frame_rate = struct.unpack('<BBHHHB', data_bytes)

            self._request_gimbal_camera_codec_specs_msg.seq = seq
            self._request_gimbal_camera_codec_specs_msg.stream_type = stream_type
            self._request_gimbal_camera_codec_specs_msg.video_enc_type = video_enc_type
            self._request_gimbal_camera_codec_specs_msg.resolution_l = res_l
            self._request_gimbal_camera_codec_specs_msg.resolution_h = res_h
            self._request_gimbal_camera_codec_specs_msg.video_bitrate = bitrate
            self._request_gimbal_camera_codec_specs_msg.video_framerate = frame_rate
            #print("codec specs msg: ",self._request_gimbal_camera_codec_specs_msg)

            return True
        except Exception as e:
            self._logger.error("Error parsing gimbal camera codec specs request: %s", e)
            self._request_gimbal_camera_codec_specs_msg.sta = 0 # Failed
            return False
        
    def parseSendGimbalCameraCodecSpecsMsg(self, msg:str, seq:int):
        """
        Parses the return message from the gimbal camera codec specs send
        """
        try:
            self._send_gimbal_camera_codec_specs_msg.seq = seq
            self._send_gimbal_camera_codec_specs_msg.stream_type = int('0x'+msg[0:2], base=16)
            self._send_gimbal_camera_codec_specs_msg.sta = bool(int('0x'+msg[2:4], base=16))
            return True
        except Exception as e:
            self._logger.error("Error parsing gimbal camera codec specs send: %s", e)
            self._send_gimbal_camera_codec_specs_msg.sta = 0 # Failed
            return False
    
    def parseRequestGimbalCameraImageModeMsg(self, msg: str, seq: int):
        """
        Parse the gimbal camera image mode request message.
        
        Parameters:
        - msg (str): Hex string of the ACK data format (e.g., "03")
        - seq (int): Sequence number to associate with this message
        """
        try:
            # Convert the hex string to integer (msg is a single byte: vdisp_mode)
            vdisp_mode = int(msg, 16)

            # Map vdisp_mode to description
            image_mode_map = {
                0: "Split Screen (Main: Zoom & Thermal. Sub: Wide Angle)",
                1: "Split Screen (Main: Wide Angle & Thermal. Sub: Zoom)",
                2: "Split Screen (Main: Zoom & Wide Angle. Sub: Thermal)",
                3: "Single Images (Main: Zoom. Sub: Thermal)",
                4: "Single Images (Main: Zoom. Sub: Wide Angle)",
                5: "Single Images (Main: Wide Angle. Sub: Thermal)",
                6: "Single Images (Main: Wide Angle. Sub: Zoom)",
                7: "Single Images (Main: Thermal. Sub: Zoom)",
                8: "Single Images (Main: Thermal. Sub: Wide Angle)",
            }

            # Assign values to the message object
            self._request_gimbal_camera_image_mode_msg.seq = seq
            self._request_gimbal_camera_image_mode_msg.vdisp_mode = vdisp_mode
            self._request_gimbal_camera_image_mode_msg.description = image_mode_map.get(vdisp_mode, "Unknown mode")

        except Exception as e:
            print(f"Failed to parse gimbal camera image mode message: {e}")
    def parseSendGimbalCameraImageModeMsg(self, msg:str, seq:int):
        """
        Parse the gimbal camera image mode send message.
        """
        vdisp_mode = int(msg, 16)

        # Map vdisp_mode to description
        image_mode_map = {
                0: "Split Screen (Main: Zoom & Thermal. Sub: Wide Angle)",
                1: "Split Screen (Main: Wide Angle & Thermal. Sub: Zoom)",
                2: "Split Screen (Main: Zoom & Wide Angle. Sub: Thermal)",
                3: "Single Images (Main: Zoom. Sub: Thermal)",
                4: "Single Images (Main: Zoom. Sub: Wide Angle)",
                5: "Single Images (Main: Wide Angle. Sub: Thermal)",
                6: "Single Images (Main: Wide Angle. Sub: Zoom)",
                7: "Single Images (Main: Thermal. Sub: Zoom)",
                8: "Single Images (Main: Thermal. Sub: Wide Angle)",
            }

        try:
            self._send_gimbal_camera_image_mode_msg.seq = seq
            self._send_gimbal_camera_image_mode_msg.vdisp_mode = vdisp_mode
            self._send_gimbal_camera_image_mode_msg.description = image_mode_map.get(vdisp_mode, "Unknown mode")
        except Exception as e:
            print(f"Failed to parse gimbal camera image mode message: {e}")
    
    
    ##################################################
    #                   Get functions                #
    ##################################################
    def getAttitude(self):
        return(self._att_msg.yaw, self._att_msg.pitch, self._att_msg.roll)

    def getAttitudeSpeed(self):
        return(self._att_msg.yaw_speed, self._att_msg.pitch_speed, self._att_msg.roll_speed)

    def getFirmwareVersion(self):
        return(self._fw_msg.gimbal_firmware_ver)

    def getHardwareID(self):
        return(self._hw_msg.id)
    
    def getCameraTypeString(self):
        return(self._hw_msg.cam_type_str)

    def getRecordingState(self):
        return(self._record_msg.state)

    def getMotionMode(self):
        return(self._motionMode_msg.mode)

    def getMountingDirection(self):
        return(self._mountDir_msg.dir)

    def getFunctionFeedback(self):
        return(self._funcFeedback_msg.info_type)

    def getZoomLevel(self):
        return(self._manualZoom_msg.level)
    
    def getCurrentZoomLevel(self):
        return(self._current_zoom_level_msg.level)
    
    def getTemperatureAtPoint(self):
        return(self._temperature_at_point_msg.temp, self._temperature_at_point_msg.x, self._temperature_at_point_msg.y)
    
    def getGimbalCameraSoftRestart(self):
        return(self._gimbal_camera_soft_restart_msg.camera_reboot_status, self._gimbal_camera_soft_restart_msg.gimbal_reboot_status)

    def getGimbalCameraCodecSpecs(self):
        return(self._request_gimbal_camera_codec_specs_msg.stream_type, self._request_gimbal_camera_codec_specs_msg.video_enc_type, self._request_gimbal_camera_codec_specs_msg.resolution_l, self._request_gimbal_camera_codec_specs_msg.resolution_h, self._request_gimbal_camera_codec_specs_msg.video_bitrate, self._request_gimbal_camera_codec_specs_msg.video_framerate)
        
    def getSendGimbalCameraCodecSpecs(self):
        return(self._send_gimbal_camera_codec_specs_msg.stream_type, self._send_gimbal_camera_codec_specs_msg.sta)

    def getCenteringFeedback(self):
        return(self._center_msg.success)
    
    def getDataStreamFeedback(self):
        return(self._request_data_stream_msg.data_type)
    
    def getGimbalCameraImageMode(self):
        return(self._request_gimbal_camera_image_mode_msg.vdisp_mode, self._request_gimbal_camera_image_mode_msg.description)

    def getRTSPURLs(self):
        if self._hw_msg.cam_type_str == 'A8 mini':
            return({"rgb": "rtsp://192.168.144.25:8554/main.264", "thermal": ""})
        elif self.getCameraTypeString() == 'ZT6' or self.getCameraTypeString() == 'ZT30':
            main_url = "rtsp://192.168.144.25:8554/video1"
            sub_url = "rtsp://192.168.144.25:8554/video2"
            
            # Request current image mode
            if not self.requestGimbalCameraImageMode():
                self._logger.warning("Failed to get image mode, defaulting to mode 3 (Single Image - Main: Zoom, Sub: Thermal)")
                return({"rgb": main_url, "thermal": sub_url})
            
            # Small delay to ensure we receive the response
            #sleep(0.1)
            
            try:
                mode, _ = self.getGimbalCameraImageMode()
                
                # Map URLs based on image mode
                if mode == 0:  # Split Screen (Main: Zoom & Thermal. Sub: Wide Angle)
                    return({"rgb": main_url, "thermal": main_url})
                elif mode == 1:  # Split Screen (Main: Wide Angle & Thermal. Sub: Zoom)
                    return({"rgb": main_url, "thermal": main_url})
                elif mode == 2:  # Split Screen (Main: Zoom & Wide Angle. Sub: Thermal)
                    return({"rgb": main_url, "thermal": sub_url})
                elif mode == 3:  # Single Image (Main: Zoom. Sub: Thermal)
                    return({"rgb": main_url, "thermal": sub_url})
                elif mode == 4:  # Single Image (Main: Zoom. Sub: Wide Angle)
                    return({"rgb": main_url, "rgb_wide": sub_url})
                elif mode == 5:  # Single Image (Main: Wide Angle. Sub: Thermal)
                    return({"rgb": main_url, "thermal": sub_url})
                elif mode == 6:  # Single Image (Main: Wide Angle. Sub: Zoom)
                    return({"rgb_wide": main_url, "rgb": sub_url})
                elif mode == 7:  # Single Image (Main: Thermal. Sub: Zoom)
                    return({"thermal": main_url, "rgb": sub_url})
                elif mode == 8:  # Single Image (Main: Thermal. Sub: Wide Angle)
                    return({"thermal": main_url, "rgb_wide": sub_url})
                else:
                    self._logger.warning(f"Unknown image mode: {mode}")
                    return(None, None)
            except (AttributeError, Exception) as e:
                self._logger.warning(f"Error getting image mode: {e}, defaulting to mode 3 (Single Image - Main: Zoom, Sub: Thermal)")
                return({"rgb": main_url, "thermal": sub_url})
        else:
            self._logger.warning("Camera not supported for RTSP URLs.")
            return(None, None)
    
    #################################################
    #                 Set functions                 #
    #################################################
    def setGimbalRotation(self, yaw, pitch, err_thresh=1.0, kp=4):
        """
        Sets gimbal attitude angles yaw and pitch in degrees

        Params
        --
        yaw: [float] desired yaw in degrees
        pitch: [float] desired pitch in degrees
        err_thresh: [float] acceptable error threshold, in degrees, to stop correction
        kp [float] proportional gain
        """
        if (pitch >25 or pitch <-90):
            self._logger.error("desired pitch is outside controllable range -90~25")
            return

        if (yaw >45 or yaw <-45):
            self._logger.error("Desired yaw is outside controllable range -45~45")
            return

        th = err_thresh
        gain = kp
        while(True):
            self.requestGimbalAttitude()
            if self._att_msg.seq==self._last_att_seq:
                self._logger.info("Did not get new attitude msg")
                self.requestGimbalSpeed(0,0)
                continue

            self._last_att_seq = self._att_msg.seq

            yaw_err = -yaw + self._att_msg.yaw # NOTE for some reason it's reversed!!
            pitch_err = pitch - self._att_msg.pitch

            self._logger.debug("yaw_err= %s", yaw_err)
            self._logger.debug("pitch_err= %s", pitch_err)

            if (abs(yaw_err) <= th and abs(pitch_err)<=th):
                self.requestGimbalSpeed(0, 0)
                self._logger.info("Goal rotation is reached")
                break

            y_speed_sp = max(min(100, int(gain*yaw_err)), -100)
            p_speed_sp = max(min(100, int(gain*pitch_err)), -100)
            self._logger.debug("yaw speed setpoint= %s", y_speed_sp)
            self._logger.debug("pitch speed setpoint= %s", p_speed_sp)
            self.requestGimbalSpeed(y_speed_sp, p_speed_sp)

            sleep(0.1) # command frequency

def test():
    cam=SIYISDK(debug=False)

    if not cam.connect():
        exit(1)

    print("Firmware version: ", cam.getFirmwareVersion())
    
    cam.requestGimbalSpeed(10,0)
    sleep(3)
    cam.requestGimbalSpeed(0,0)
    print("Attitude: ", cam.getAttitude())
    cam.requestCenterGimbal()

    val=cam.getRecordingState()
    print("Recording state: ",val)
    cam.requestRecording()
    sleep(0.1)
    val=cam.getRecordingState()
    print("Recording state: ",val)
    cam.requestRecording()
    sleep(0.1)
    val=cam.getRecordingState()
    print("Recording state: ",val)

    print("Taking photo...")
    cam.requestPhoto()
    sleep(1)
    print("Feedback: ", cam.getFunctionFeedback())

    cam.disconnect()

if __name__=="__main__":
    test()