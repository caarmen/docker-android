import logging
import os
import subprocess
import concurrent.futures
import uuid

from device import Genymotion, DeviceType
from helper import get_env_value_or_raise
from constants import ENV, UTF8

class GenySAAS(Genymotion):
    def __init__(self) -> None:
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.device_type = DeviceType.GENY_SAAS.value
        self.created_devices = []
    
    def login(self) -> None:
        if os.getenv(ENV.GENY_AUTH_TOKEN):
            auth_token = get_env_value_or_raise(ENV.GENY_AUTH_TOKEN)
            subprocess.check_call(f"gmsaas auth token {auth_token} > /dev/null 2>&1", shell=True)
        else:
            user = get_env_value_or_raise(ENV.GENY_SAAS_USER)
            password = get_env_value_or_raise(ENV.GENY_SAAS_PASS)
            subprocess.check_call(f"gmsaas auth login {user} {password} > /dev/null 2>&1", shell=True)
        self.logger.info("successfully logged in!")
    
    def create(self) -> None:
        super().create()
        
        # Collect all items first
        items = []
        # CARM: could be good to put this part into a function which returns the items.
        #       Pycharm reports, later, inside create_instance: "Shadows name 'local_port' from outer scope"
        for item in self.get_data_from_template(ENV.GENY_SAAS_TEMPLATE_FILE_NAME):
            name = ""
            template = ""
            local_port = ""
            # implement like this because local_port param is not a must
            for k, v in item.items():
                if k.lower() == "name":
                    name = v
                elif k.lower() == "template":
                    template = v
                elif k.lower() == "local_port":
                    local_port = v
                else:
                    self.logger.warning(f"'{k}' is not supported! Please check the documentation!")
            
            if not name:
                name = str(uuid.uuid4())
            
            if not template:
                self.shutdown_and_logout()
                raise RuntimeError(f"'template' is a must parameter and not given!")

            # A dataclass could be more explicit:
            # @dataclasses.dataclass
            # class DeviceConfig:
            #     name: str
            #     template: str
            #     local_port: int
            #
            # items.append(DeviceConfig(name=name, template=template, local_port=local_port)
            #
            # One advantage is that we could then put this data class as a type hint the create_instance signature
            items.append({
                'name': name,
                'template': template,
                'local_port': local_port
            })
        
        def create_instance(item_data):
            """Create a single instance and return the result"""
            # CARM with a dataclass passed as an arg, no need to extract the fields one by one.
            name = item_data['name']
            template = item_data['template']
            local_port = item_data['local_port']
            
            self.logger.info(f"name: {name}, template: {template}")
            creation_cmd = f"gmsaas instances start {template} {name}"
            
            try:
                instance_id = subprocess.check_output(creation_cmd.split()).decode(UTF8).replace("\n", "")
                
                # Connect to ADB
                additional_args = ""
                if local_port:
                    additional_args = f"--adb-serial-port {local_port}"
                connect_cmd = f"gmsaas instances adbconnect {instance_id} {additional_args}"
                subprocess.check_call(f"{connect_cmd}", shell=True)

                # CARM: this would be more explicit to return a dataclass instead of a dict, for the result.
                #
                # @dataclasses.dataclass
                # class InstanceResult:
                #     name: str
                #     instance_id: str
                #
                # Then:
                # return InstanceResult(name=name, instance_id=instance_id)
                return {f"{name}": instance_id}
                
            except Exception as e:
                self.logger.error(f"Failed to create instance {name}: {e}")
                raise e
        
        # Process items in parallel (workers based on items count)
        max_workers = len(items)
        # CARM: set a max to max_workers: if the json file has a huge list of recipes to start, it could
        # end up creating too many threads. Maybe cap it to 100.
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            try:
                # Submit all tasks
                future_to_item = {executor.submit(create_instance, item): item for item in items}
                
                # Collect results as they complete
                for future in concurrent.futures.as_completed(future_to_item):
                    item = future_to_item[future]
                    try:
                        created_device = future.result()
                        self.created_devices.append(created_device)
                        self.logger.info(f"Successfully created device: {created_device}")
                    except Exception as e:
                        self.logger.error(f"Instance creation failed for {item['name']}: {e}")
                        self.shutdown_and_logout()
                        exit(1)
                        
            except Exception as e:
                self.shutdown_and_logout()
                self.logger.error(f"Parallel execution failed: {e}")
                exit(1)
    
    def shutdown_and_logout(self) -> None:
        if bool(self.created_devices):
            self.logger.info("Created device(s) will be removed!")
            
            def stop_instance(device_info):
                """Stop a single instance"""
                # CARM: maybe put some more descriptive variable names:
                # n => device_name
                # i => device_uuid
                # Actually: This would also be clearer with the data class. I think we only have one
                # device to stop here, right?
                for n, i in device_info.items():
                    try:
                        subprocess.check_call(f"gmsaas instances stop {i}", shell=True)
                        self.logger.info(f"device '{n}' is successfully removed!")
                        return True
                    except Exception as e:
                        self.logger.error(f"Failed to stop device '{n}': {e}")
                        return False
            
            # Stop all devices in parallel
            # CARM same remark for max_workers here
            max_workers = len(self.created_devices)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all stop tasks
                futures = [executor.submit(stop_instance, device) for device in self.created_devices]
                
                # Wait for all to complete
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        self.logger.error(f"Error during device shutdown: {e}")
        
        # Logout after all devices are stopped
        if os.getenv(ENV.GENY_AUTH_TOKEN):
            subprocess.check_call("gmsaas auth reset", shell=True)
        else:
            subprocess.check_call("gmsaas auth logout", shell=True)
        self.logger.info("successfully logged out!")