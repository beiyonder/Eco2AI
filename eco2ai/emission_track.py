import os
import time
import platform
import pandas as pd
import numpy as np
import uuid
import warnings
import tzlocal
from apscheduler.schedulers.background import BackgroundScheduler

from eco2ai.tools.tools_gpu import GPU, all_available_gpu
from eco2ai.tools.tools_cpu import CPU, all_available_cpu
from eco2ai.tools.tools_ram import RAM
from eco2ai.utils import (
    is_file_opened,
    define_carbon_index,
    get_params,
    set_params,
    # calculate_money,
    # summary,
    encode,
    encode_dataframe,
    electricity_pricing_check,
    calculate_price,
    FileDoesNotExistsError,
    NotNeededExtensionError,
)

from pandas.api.types import is_numeric_dtype, is_string_dtype, is_categorical_dtype, is_bool_dtype, is_object_dtype

FROM_mWATTS_TO_kWATTH = 1000 * 1000 * 3600
FROM_kWATTH_TO_MWATTH = 1000
__version__ = '0.3.12'

class IncorrectMethodSequenceError(Exception):
    pass


class Tracker:
    """
    This class calculates CO2 emissions during CPU or GPU calculations
    In order to calculate CPU & GPU power consumption correctly you should create the 'Tracker' before any CPU or GPU usage
    It is recommended to create a new “Tracker” object per every new calculation.

    Example
    ----------
    import eco2ai.Tracker
    tracker = eco2ai.Tracker()

    tracker.start()

    *your CPU and GPU calculations*

    tracker.stop()

    """

    def __init__(
        self,
        project_name=None,
        experiment_description=None,
        file_name=None,
        measure_period=10,
        emission_level=None,
        alpha_2_code=None,
        region=None,
        cpu_processes="current",
        pue=1,
        encode_file=None,
        electricity_pricing=None,
        ignore_warnings=False,
        timezone=False,
    ):
        """
        This class method initializes a Tracker object and creates fields of class object

        Parameters
        ----------
        project_name: str
            Specified by user project name.
            The default is None
        experiment_description: str
            Specified by user experiment description.
            The default is None
        file_name: str
            Name of file to save the the results of calculations.
            The default is None
        measure_period: float
            Period of power consumption measurements in seconds.
            The more period the more time between measurements.
            The default is 10
        emission_level: float
            The mass of CO2 in kilos, which is produced  per every MWh of consumed energy.
            The default is None
        alpha_2_code: str
            User specified country code.
            User can search own country code here: https://www.iban.com/country-codes
            Default is None
        region: str
            User specified country region/state/district.
            Default is None
        cpu_processes: str
            if cpu_processes == "current", then calculates CPU utilization percent only for the current running process
            if cpu_processes == "all", then calculates full CPU utilization percent(sum of all running processes)
        pue: float
            Power utilization efficiency.
            It is ration of the total 'facility power' and 'IT equipment energy consumption'.
            PUE is a measure of a data center power efficiency.
            This parameter will be very essential during calculations using data centres facilities.
            The default is 1.
        encode_file: str
            If this parameter is not None, results of calculations will be encoded
            and the results will be written to file.
            If this parameter == True encoded data will be written to file "encoded_" + value of file_name parameter.
            So, default name of file with encoded data will be "encoded_emission.csv"
            If this parameter is of str type, then name of file with encoded data will be value of encode_file parameter.
            The default is None.
        electricity_pricing: dict
            Dictionary with time intervals as keys and electricity price during that intervals as values.
            Electricity price should be set without any currency designation.
            Every interval must be constructed as follows:
                1) "hh:mm-hh:mm", hh - hours, mm - minutes. hh in [0, ..., 23], mm in [0, ..., 59]
                2) Intervals should be consistent: they mustn't overlap and they should in chronological order.
                Instance of consistent intervals: "8:30-19:00", "19:00-6:00", "6:00-8:30"
                Instance of inconsistent intervals: "8:30-20:00", "18:00-3:00", "6:00-12:30"
                3) Total duration of time intervals in hours must be 24 hours(1 day).
        ignore_warnings: bool
            If true, then user will be notified of all the warnings. If False, there won't be any warnings.
            The default is False.
        timezone: directly set defauls timezone. Can be useful if timezone can not be automatically detected

        Returns
        -------
        Tracker: Tracker
            Object of class Tracker

        """
        self._ignore_warnings = ignore_warnings
        if not self._ignore_warnings:
            warnings.warn(
                message="""
If you use a VPN, you may have problems with identifying your country by IP.
It is recommended to disable VPN or
manually set up the ISO-Alpha-2 code of your country during initialization of the Tracker() class.
You can find the ISO-Alpha-2 code of your country here: https://www.iban.com/country-codes
"""
            )
        if (isinstance(measure_period, int) or isinstance(measure_period, float)) and measure_period <= 0:
            raise ValueError("'measure_period' should be positive number")
        if encode_file is not None:
            if not isinstance(encode_file, str) and not encode_file:
                raise TypeError(f"'encode_file' parameter should have str type, not {type(encode_file)}")
            if isinstance(encode_file, str) and not encode_file.endswith(".csv"):
                raise NotNeededExtensionError("'encode_file' name need to be with extension '.csv'")
        if file_name is not None:
            if isinstance(file_name, str) and not file_name:
                raise TypeError(f"'file_name' parameter should have str type, not {type(file_name)}")
            if isinstance(file_name, str) and not file_name.endswith(".csv"):
                raise NotNeededExtensionError("'file_name' name need to be with extension '.csv'")
        self._params_dict = get_params()
        self.project_name = project_name if project_name is not None else self._params_dict["project_name"]
        self.experiment_description = (
            experiment_description
            if experiment_description is not None
            else self._params_dict["experiment_description"]
        )
        self.file_name = file_name if file_name is not None else self._params_dict["file_name"]
        self._measure_period = measure_period if measure_period is not None else self._params_dict["measure_period"]
        self._pue = pue if pue is not None else self._params_dict["pue"]
        self.get_set_params(
            self.project_name, self.experiment_description, self.file_name, self._measure_period, self._pue
        )

        self._emission_level, self._country = define_carbon_index(emission_level, alpha_2_code, region)
        self._cpu_processes = cpu_processes
        if not timezone:
            timezone = str(tzlocal.get_localzone())

        self._scheduler = BackgroundScheduler(
            job_defaults={"max_instances": 10}, timezone=timezone, misfire_grace_time=None
        )
        self._start_time = None
        self._cpu = None
        self._gpu = None
        self._ram = None
        self._id = None
        self._current_epoch = "N/A"
        self._consumption = 0
        self._encode_file = encode_file if not encode_file else "encoded_" + file_name
        electricity_pricing_check(electricity_pricing)
        self._electricity_pricing = electricity_pricing
        self._total_price = 0
        self._os = platform.system()
        if self._os == "Darwin":
            self._os = "MacOS"
        # self._mode == "first_time" means that the Tracker is just initialized
        # self._mode == "run time" means that CO2 tracker is now running
        # self._mode == "shut down" means that CO2 tracker is stopped
        # self._mode == "training" means that CO2 tracker tracks training process
        self._mode = "first_time"
        # parameters to save during model training
        self._parameters_to_save = ""

    def get_set_params(
        self, project_name=None, experiment_description=None, file_name=None, measure_period=None, pue=None
    ):
        """
        This function returns default Tracker attributes values:
        project_name = ...
        experiment_description = ...
        file_name = ...
        measure_period = ...
        pue = ...

        Parameters
        ----------
        project_name: str
            Specified by user project name.
            The default is None
        experiment_description: str
            Specified by user experiment description.
            The default is None
        file_name: str
            Name of file to save the the results of calculations.
            The default is None
        measure_period: float
            Period of power consumption measurements in seconds.
            The more period the more time between measurements.
            The default is None
        pue: float
            Power utilization efficiency.
            It is ration of the total 'facility power' and 'IT equipment energy consumption'.
            PUE is a measure of a data center power efficiency.
            This parameter will be very essential during calculations using data centres facilities.
            The default is None

        Returns
        -------
        dictionary: dict


        """
        dictionary = dict()
        if project_name is not None:
            dictionary["project_name"] = project_name
        else:
            dictionary["project_name"] = "default project name"
        if experiment_description is not None:
            dictionary["experiment_description"] = experiment_description
        else:
            dictionary["experiment_description"] = "default experiment description"
        if file_name is not None:
            dictionary["file_name"] = file_name
        else:
            dictionary["file_name"] = "emission.csv"
        if measure_period is not None:
            dictionary["measure_period"] = measure_period
        else:
            dictionary["measure_period"] = 10
        if pue is not None:
            dictionary["pue"] = pue
        else:
            dictionary["pue"] = 1
        set_params(**dictionary)

        return dictionary

    def consumption(self):
        """
        This class method returns consumption

        Parameters
        ----------
        No parameters

        Returns
        -------
        consumption: float
            Power consumption of every device in a system.

        """
        return self._consumption

    def price(self):
        """
        This class method returns total electricity price

        Parameters
        ----------
        No parameters

        Returns
        -------
        total_price: float
            Total price for electrical power spent.

        """
        return self._total_price

    def id(self):
        """
        This class method returns the Tracker id

        Parameters
        ----------
        No parameters

        Returns
        -------
        id: str
            The Tracker's id. id is random UUID

        """
        return self._id

    def emission_level(self):
        """
        This class method returns emission level

        Parameters
        ----------
        No parameters

        Returns
        -------
        emission_level: float
            emission_level is the mass of CO2 in kilos, which is produced  per every MWh of consumed energy.

        """
        return self._emission_level

    def measure_period(self):
        """
        This class method returns measure period of Tracker

        Parameters
        ----------
        No parameters

        Returns
        -------
        measure_period: float
            Period of power consumption measurements.
            The more period the more time between measurements.
            The default is 10

        """
        return self._measure_period

    def _construct_attributes_dict(
        self,
    ):
        """
        This class method constructs dictionary with the following keys:
        Results is a table with the following columns:
            project_name
            experiment_description(model type etc.)
            start_time
            duration(s)
            power_consumption(kWTh)
            CO2_emissions(kg)
            CPU_name
            GPU_name
            OS
            region/country

        Parameters
        ----------
        No parameters

        Returns
        -------
        attributes_dict: dict
            Dictionary with all the attributes that should be written to .csv file

        """
        # if user used older versions, it may be needed to upgrade his .csv file
        # but after all, such verification should be deleted
        # self.check_for_older_versions()
        attributes_dict = dict()
        attributes_dict["id"] = [self._id]
        attributes_dict["project_name"] = [f"{self.project_name}"]
        attributes_dict["experiment_description"] = [f"{self.experiment_description}"]
        attributes_dict["epoch"] = [
            "epoch: " + str(self._current_epoch) + str(self._parameters_to_save) if self._mode == "training" else "N/A"
        ]
        attributes_dict["start_time"] = [f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._start_time))}"]
        attributes_dict["duration(s)"] = [f"{time.time() - self._start_time}"]
        attributes_dict["power_consumption(kWh)"] = [f"{self._consumption}"]
        attributes_dict["CO2_emissions(kg)"] = [f"{self._consumption * self._emission_level / FROM_kWATTH_TO_MWATTH}"]
        attributes_dict["CPU_name"] = [f"{self._cpu.name()}/{self._cpu.cpu_num()} device(s), TDP:{self._cpu.tdp()}"]
        attributes_dict["GPU_name"] = [f"{self._gpu.name()} {self._gpu.gpu_num()} device(s)"]
        attributes_dict["OS"] = [f"{self._os}"]
        attributes_dict["region/country"] = [f"{self._country}"]
        attributes_dict["cost"] = [f"{self._total_price}"]

        return attributes_dict

    def align_value_to_dtype(self, value, dtype):
        """
        Align a single value to the given pandas dtype.
        Returns the value if compatible, or np.nan for incompatible types.
        """
        if is_numeric_dtype(dtype):
            # Check for numeric values
            if isinstance(value, (int, float, np.number)):
                return value
        elif is_string_dtype(dtype) or is_object_dtype(dtype):
            # Check for string or object-like values
            if isinstance(value, str):
                return value
        elif is_bool_dtype(dtype):
            # Check for boolean values
            if isinstance(value, bool):
                return value
        elif is_categorical_dtype(dtype):
            # Check for categorical types
            return value  # Assume categorical data will be handled by pandas
        else:
            # Handle other types like datetime, intervals, etc., as needed
            return np.nan

        # If none of the above checks pass, replace with np.nan
        return np.nan

    def _write_to_csv(
        self,
        add_new=False,
    ):
        """
        This class method writes to .csv file calculation results.
        Results is a table with the following columns:
            project_name
            experiment_description(model type etc.)
            start_time
            duration(s)
            power_consumption(kWTh)
            CO2_emissions(kg)
            CPU_name
            GPU_name
            OS
            region/country

        Parameters
        ----------
        add_new: bool
            Parameter, defining if function should add additional row to the dataframe
            "add_new" == True when new epoch in training was started
        parameters_to_save: str
            String with parameters user wants to save.
            The string come from ".new_epoch" method.

        Returns
        -------
        attributes_dict: dict
            Dictionary with all the attributes that should be written to .csv file

        """
        # if user used older versions, it may be needed to upgrade his .csv file
        # but after all, such verification should be deleted
        # self.check_for_older_versions()
        attributes_dict = self._construct_attributes_dict()

        if not os.path.isfile(self.file_name):
            while True:
                if not is_file_opened(self.file_name):
                    open(self.file_name, "w").close()
                    tmp = open(self.file_name, "w")
                    pd.DataFrame(attributes_dict).to_csv(self.file_name, index=False)
                    tmp.close()
                    break
                else:
                    time.sleep(0.5)

        else:
            while True:
                if not is_file_opened(self.file_name):
                    # Open the file to prevent other processes from using it
                    with open(self.file_name, "r"):
                        attributes_dataframe = pd.read_csv(self.file_name)

                        # Convert attributes_dict values into a flat list
                        attributes_array = []
                        for element in attributes_dict.values():
                            attributes_array += element

                        # # Ensure consistent types
                        # attributes_array = [
                        #     self.align_value_to_dtype(val, dtype)
                        #     for val, dtype in zip(attributes_array, attributes_dataframe.dtypes)
                        # ]

                        if attributes_dataframe[attributes_dataframe["id"] == self._id].shape[0] == 0:
                            # Adding a new row
                            attributes_dataframe.loc[len(attributes_dataframe)] = attributes_array
                        else:
                            # Updating or inserting a row
                            row_index = attributes_dataframe[attributes_dataframe["id"] == self._id].index.values[-1]

                            # check, if it's necessary to add a new row to the dataframe
                            if add_new:
                                # Insert a new row
                                attributes_dataframe = pd.DataFrame(
                                    np.vstack(
                                        (
                                            attributes_dataframe.values[: row_index + 1],
                                            attributes_array,
                                            attributes_dataframe.values[row_index + 1 :],
                                        )
                                    ),
                                    columns=attributes_dataframe.columns,
                                )
                            else:
                                # Update the existing row
                                attributes_dataframe.loc[row_index] = attributes_array

                        # Save updated DataFrame to file
                        attributes_dataframe.to_csv(self.file_name, index=False)
                    break
                else:
                    time.sleep(0.5)

        self._mode = "run time" if self._mode != "training" else "training"
        return attributes_dict

    def _update_to_new_version(self, attributes_dataframe, new_columns):
        """
         This class method is a function, that updates dataframe to newer versions: adds new columns etc

         Parameters
         ----------
         attributes_dataframe: pd.DataFrame
             Dataframe to update
         new_columns: list
             New columns which should be contained in updated dataframe

         Returns
         -------
        dataframe: pd.DataFrame
         Updated dataframe.

        """
        current_columns = list(attributes_dataframe.columns)
        for column in new_columns:
            if column not in current_columns:
                attributes_dataframe[column] = "N/A"
        attributes_dataframe = attributes_dataframe[new_columns]

        return attributes_dataframe

    def _func_for_sched(self, add_new=False):
        """
        This class method is a function, that is put in a scheduler and
        is run during a Tracker work with period "measure_period"(The Tracker class parameter).
        It calculates CPU, GPU and RAM power consumption and writes results to a .csv file.

        Parameters
        ----------
        add_new: bool
            Parameter, defining if function should add additional row to the dataframe
            "add_new" == True when new epoch in training was started

        Returns
        -------
        attributes_dict: dict
            Dictionary with all the attributes that should be written to .csv file

        """
        cpu_consumption = self._cpu.calculate_consumption()
        ram_consumption = self._ram.calculate_consumption()
        if self._gpu.is_gpu_available:
            gpu_consumption = self._gpu.calculate_consumption()
        else:
            gpu_consumption = 0
        tmp_consumption = 0
        tmp_consumption += cpu_consumption
        tmp_consumption += gpu_consumption
        tmp_consumption += ram_consumption
        tmp_consumption *= self._pue
        if self._electricity_pricing is not None:
            self._total_price += calculate_price(self._electricity_pricing, tmp_consumption)
        self._consumption += tmp_consumption

        # self._consumption = 0
        # self._start_time = time.time()
        if self._mode == "shut down":
            self._scheduler.remove_job("job")
            self._scheduler.shutdown()
        # self._write_to_csv returns attributes_dict
        return self._write_to_csv(add_new)

    def start_training(self, start_epoch=1):
        """
        This class method starts the Tracker work and signalize that it should track the training process.
        It initializes fields of CPU and GPU classes,
        IMPORTANT: during training tracking all the calculations is written to file only after ".new_epoch" method was run

        Parameters
        ----------
        start_epoch: int
            Number of epoch a training should start with.

        Returns
        -------
        No returns

        """
        if not isinstance(start_epoch, int):
            raise TypeError(f'"start_epoch" parameter must be of int type. Now, it is {type(start_epoch)}')

        self._mode = "training"

        self._current_epoch = start_epoch
        self._cpu = CPU(cpu_processes=self._cpu_processes, ignore_warnings=self._ignore_warnings)
        self._gpu = GPU(ignore_warnings=self._ignore_warnings)
        self._ram = RAM(ignore_warnings=self._ignore_warnings)
        self._id = str(uuid.uuid4())
        self._start_time = time.time()

    def new_epoch(self, parameters_dict):
        """
        This class method starts tracking new epoch.
        It calls "._func_for_sched" method, and signalize that new row should be created and added to the dataframe

        Parameters
        ----------
        parameters_dict: dict
            Dictionary with parameters user wants to save during current epoch

        Returns
        -------
        No returns

        """
        if self._mode != "training":
            raise IncorrectMethodSequenceError(
                'You can run method ".new_epoch" only after method ".start_training" was run'
            )
        self._parameters_to_save = ", "
        for key in parameters_dict:
            self._parameters_to_save += key + ": "
            self._parameters_to_save += str(parameters_dict[key]) + ", "
        # self._func_for_sched returns attributes_dict.
        attributes_dict = self._func_for_sched(add_new=True)
        # We put it into self._func_for_encoding method in order to encode calculations
        if self._encode_file:
            self._func_for_encoding(attributes_dict)
        self._current_epoch += 1
        self._parameters_to_save = ""
        self._consumption = 0
        self._total_price = 0
        self._start_time = time.time()
        if self._encode_file is not None:
            self._func_for_encoding(attributes_dict)
        self._consumption = 0

    def start(self):
        """
        This class method starts the Tracker work. It initializes fields of CPU and GPU classes,
        initializes scheduler, puts the self._func_for_sched function into it and starts its work.

        Parameters
        ----------
        No parameters

        Returns
        -------
        No returns

        """
        if self._mode == "training":
            raise IncorrectMethodSequenceError(
                """
You have already run ".start_training" method.
Please, use the interface for training: ".start_training", ".new_epoch", and "stop_training"
                """
            )
        if self._start_time is not None:
            try:
                self._scheduler.remove_job("job")
                self._scheduler.shutdown()
            except:
                pass
            self._scheduler = BackgroundScheduler(job_defaults={"max_instances": 10}, misfire_grace_time=None)
        self._cpu = CPU(cpu_processes=self._cpu_processes, ignore_warnings=self._ignore_warnings)
        self._gpu = GPU(ignore_warnings=self._ignore_warnings)
        self._ram = RAM(ignore_warnings=self._ignore_warnings)
        self._id = str(uuid.uuid4())
        self._mode = "first_time"
        self._start_time = time.time()
        self._scheduler.add_job(self._func_for_sched, "interval", seconds=self._measure_period, id="job")
        self._scheduler.start()

    def stop_training(
        self,
    ):
        """
        This class method stops the Tracker work during a training process.
        It also writes to file final calculation results.

        Parameters
        ----------
        No returns

        Returns
        -------
        No returns

        """
        # remove job from scheduler
        if self._mode != "training" or self._start_time is None:
            raise IncorrectMethodSequenceError(
                """
You should run ".start_training" method before ".stop_training" method
                """
            )
        self._consumption = 0
        self._mode = "shut down"

    def stop(
        self,
    ):
        """
        This class method stops the Tracker work, removes self._func_for_sched from the scheduler
        and stops its work, it also writes to file final calculation results.

        Parameters
        ----------
        No parameters

        Returns
        -------
        No returns

        """
        if self._mode == "training":
            self.stop_training()
            return
        if self._start_time is None:
            raise Exception("Need to first start the tracker by running tracker.start() or tracker.start_training()")
        self._scheduler.remove_job("job")
        self._scheduler.shutdown()
        self._func_for_sched()
        attributes_dict = self._write_to_csv()
        if self._encode_file is not None:
            self._func_for_encoding(attributes_dict)
        self._start_time = None
        self._consumption = 0
        self._mode = "shut down"

    def _func_for_encoding(self, attributes_dict):
        """
        This function encodes all calculated data and attributes and writes it to file.
        File name depends on 'encode_file' parameter.
        More details on file name can be seen in 'encode_file' parameter description in the Tracker class.

        Parameters
        ----------
        attributes_dict: dict
            Dictionary with all the attributes that should be written to .csv file

        Returns
        -------
        No returns

        """

        for key in attributes_dict.keys():
            # attributes_dict[key] = [encode(str(attributes_dict[key][0]))]
            attributes_dict[key] = [encode(str(value)) for value in attributes_dict[key]]

        if not os.path.isfile(self._encode_file):
            while True:
                if not is_file_opened(self._encode_file):
                    open(self._encode_file, "w").close()
                    tmp = open(self._encode_file, "r")
                    pd.DataFrame(attributes_dict).to_csv(self._encode_file, index=False)

                    tmp.close()
                    break
                else:
                    time.sleep(0.5)

        else:
            while True:
                if not is_file_opened(self._encode_file):
                    tmp = open(self._encode_file, "r")

                    attributes_dataframe = pd.read_csv(self._encode_file)

                    attributes_dataframe = pd.concat(
                        [
                            attributes_dataframe,
                            pd.DataFrame(attributes_dict),
                        ],
                        ignore_index=True,
                        axis=0,
                    )

                    attributes_dataframe.to_csv(self._encode_file, index=False)
                    tmp.close()
                    break
                else:
                    time.sleep(0.5)


def track(func):
    """
    This function is a decorator, that modifies any function by creating Tracker object and
    running Tracker.start() in the beginning of the function and Tracker.stop() in the end of function.

    Parameters
    ----------
    func: function
        Any function user wants to modify.

    Returns
    -------
    No returns.

    """

    # def inner(*args, **kwargs):
    #     tracker = Tracker()
    #     tracker.start()
    #     try:
    #         returned = func(*args, **kwargs)
    #     except Exception:
    #         tracker.stop()
    #         del tracker
    #         raise Exception
    #     tracker.stop()
    #     del tracker
    #     return returned

    def inner(*args, **kwargs):
        tracker = Tracker()
        tracker.start()
        try:
            returned = func(*args, **kwargs)
        except Exception as e:
            tracker.stop()  # Ensure the tracker stops even on an exception
            raise  # Re-raise the original exception with full context
        finally:
            tracker.stop()  # Ensure the tracker stops no matter what
        return returned

    return inner
