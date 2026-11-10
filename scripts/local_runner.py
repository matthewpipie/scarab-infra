#!/usr/bin/python3

# 01/27/2025 | Surim Oh | local_runner.py

import os
import subprocess
import psutil
import signal
import re
import time
import heapq
import traceback
from pathlib import Path
from .utilities import (
    confirm_kill,
        err,
        info,
        get_simpoints,
        write_docker_command_to_file,
        remove_docker_containers,
        prepare_simulation,
        finish_simulation,
        get_image_list,
        get_docker_prefix,
        prepare_trace,
        finish_trace,
        write_trace_docker_command_to_file,
        get_weight_by_cluster_id,
        image_exist,
        check_can_skip,
        get_old_job_logs,
        remove_old_job_logs,
        print_simulation_status_summary,
        normalize_simulations,
        require_aslr_disabled,
        use_apptainer,
        resolve_max_parallel,
        experiment_name_regex,
        pool_name_regex,
        pool_eligible,
        pool_container_name,
        remove_pool_containers,
        remove_tmp_run_scripts,
        )
from .image_identity import image_tag_for

# Check if a container is running on local
# Inputs: docker_prefix, job_name, user,
# Output: list of containers
def check_docker_container_running(docker_prefix_list, job_name, user, dbg_lvl, configs=None):
    """Names of the experiment's running simulations.

    A sim counts as running while its job script (`bash <name>_tmp_run.sh`) is alive, or while
    its own container is up. The script is what matters: with container_reuse a sim has no
    container of its own, and without it a sim whose `docker run` is still queued behind the
    docker daemon has no running container yet, and used to be reported as failed.
    """
    pattern = experiment_name_regex(docker_prefix_list, job_name, user, configs)
    pool_pattern = pool_name_regex(docker_prefix_list, job_name, user)
    running = set()
    for proc in psutil.process_iter(attrs=['cmdline']):
        for arg in proc.info.get('cmdline') or []:
            name = os.path.basename(arg)
            if name.endswith("_tmp_run.sh"):
                name = name[:-len("_tmp_run.sh")]
                if pattern.match(name):
                    running.add(name)
    if use_apptainer():  # no containers to list
        return sorted(running)
    try:
        dockers = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, check=True)
    except Exception as e:
        err(f"Error while listing running docker containers for {job_name}", dbg_lvl)
        raise e
    for line in dockers.stdout.split():
        if pattern.match(line) and not pool_pattern.match(line):
            running.add(line)
    return sorted(running)

def running_pool_containers(docker_prefix_list, job_name, user):
    if use_apptainer():
        return []
    pattern = pool_name_regex(docker_prefix_list, job_name, user)
    dockers = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, check=False)
    return sorted(n for n in dockers.stdout.split() if pattern.match(n))

# Print info/status of running experiment and available cores
def print_status(user, job_name, docker_prefix_list, descriptor_data=None, workloads_data=None, dbg_lvl = 2):
    configs = list(descriptor_data["configurations"]) if descriptor_data else None
    docker_running = check_docker_container_running(docker_prefix_list, job_name, user, dbg_lvl, configs)
    pools = running_pool_containers(docker_prefix_list, job_name, user)
    if len(docker_running) > 0:
        print(f"\033[92mRUNNING:     local\033[0m")
        for docker in docker_running:
            print(f"\033[92m    SIMULATION: {docker}\033[0m")
    else:
        print(f"\033[31mNOT RUNNING: local\033[0m")
    if pools:
        print(f"    Reused containers (container_reuse): {len(pools)}")

    if descriptor_data is None or workloads_data is None:
        return
    print_simulation_status_summary(
        descriptor_data,
        workloads_data,
        docker_prefix_list,
        user,
        docker_running,
        [],
        dbg_lvl=dbg_lvl,
        log_file_count_buffer=0,
        strict_log_count=False,
        log_count_offset=0,
        prep_failed_label="Failed - Prep",
    )

def kill_jobs(user, job_type, job_name, docker_prefix_list, infra_dir, dbg_lvl, configs=None):
    # Define the process name pattern
    if job_type == "simulation":
        pattern = re.compile(f"python3 -m scripts.run_simulation -dbg 3 -d {infra_dir}/json/{job_name}.json")
        sci_action = "--sim"
    elif job_type == "trace":
        pattern = re.compile(f"python3 -m scripts.run_trace -dbg 3 -d {infra_dir}/json/{job_name}.json")
        sci_action = "--trace"
    script_pattern = experiment_name_regex(docker_prefix_list, job_name, user, configs, suffix="_tmp_run.sh")
    # Never this process or the sci that started it (an exception in run_simulation lands here).
    me = {os.getpid()} | {p.pid for p in psutil.Process().parents()}

    def is_sci_runner(cmdline):
        # `./sci --sim <descriptor>` and the conda python it re-executes itself under.
        if not any(os.path.basename(a) == "sci" for a in cmdline[:2]):
            return False
        return any(a == sci_action and (b == job_name or (b.endswith(".json") and Path(b).stem == job_name))
                   for a, b in zip(cmdline, cmdline[1:]))

    # Iterate over all processes
    found_process = []
    job_scripts = []
    for proc in psutil.process_iter(attrs=['pid', 'name', 'username', 'cmdline']):
        try:
            # Ensure 'cmdline' exists and is iterable
            cmdline = proc.info.get('cmdline', [])
            if proc.info.get('pid') in me or proc.info.get('username') != user:
                continue
            # Check if the process name matches the pattern and is run by the specified user
            if cmdline and isinstance(cmdline, list):
                cmdline_str = " ".join(cmdline)
                if pattern.match(cmdline_str) or is_sci_runner(cmdline):
                    found_process.append(proc)
                    print(f"Found process {proc.info.get('name')} with PID {proc.info.get('pid')} running by {user}: {cmdline_str}")
                elif any(script_pattern.match(os.path.basename(a)) for a in cmdline):
                    job_scripts.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Handle processes that may have been terminated or we don't have permission to access
            continue

    # A job script left by a runner killed earlier keeps its sim going, and with container_reuse
    # it would start its slot's container again after the cleanup below.
    runner_children = set()
    for proc in found_process:
        try:
            runner_children.update(c.pid for c in proc.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    orphans = [p for p in job_scripts if p.pid not in runner_children]
    if orphans:
        print(f"Found {len(orphans)} job script(s) of {job_name} with no runner")

    print(str(len(found_process)) + "found.")
    if confirm_kill("Do you want to kill these processes?"):
        # Iterate over found processes
        for proc in found_process + orphans:
            try:
                # Kill the process and all its child processes
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                        print(f"Killed child process with PID {child.pid}")
                    except (psutil.NoSuchProcess):
                        print(f"No such process {child.pid}")
                        continue
                    except (psutil.AccessDenied):
                        print(f"Access denied to {child.pid}")
                        continue
                proc.kill()
                print(f"Terminated process {proc.info['name']} and its children.")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # Handle processes that may have been terminated or we don't have permission to access
                continue
    else:
        print("Operation canceled.")

    info(f"Clean up docker containers..", dbg_lvl)
    remove_docker_containers(docker_prefix_list, job_name, user, dbg_lvl, configs)

    info(f"Removing temporary run scripts..", dbg_lvl)
    remove_tmp_run_scripts(os.getcwd(), job_name, user, dbg_lvl, configs)

def run_simulation(user, descriptor_data, workloads_data, infra_dir, descriptor_path, dbg_lvl = 1):
    architecture = descriptor_data["architecture"]
    experiment_name = descriptor_data["experiment"]
    docker_home = descriptor_data["root_dir"]
    scarab_path = descriptor_data["scarab_path"]
    scarab_build = descriptor_data["scarab_build"]
    traces_dir = descriptor_data["traces_dir"]
    configs = descriptor_data["configurations"]
    simulations = descriptor_data["simulations"]
    application_dir = descriptor_data.get("application_dir", ".")

    docker_prefix_list = get_image_list(simulations, workloads_data)

    max_processes = resolve_max_parallel(descriptor_data)
    info(f"Running at most {max_processes} simulations at once (max_parallel; host has {os.cpu_count()} logical CPUs)", dbg_lvl)
    # container_reuse: each of the max_processes slots keeps one container and runs its sims in
    # it one after another (see POOL_SIM_MODES in utilities.py). A process holds its slot from
    # launch until it is reaped, so a slot's container never has two job scripts.
    container_reuse = bool(descriptor_data.get("container_reuse", False))
    if container_reuse:
        info("container_reuse: trace sims run in one reused container per slot", dbg_lvl)
    free_slots = list(range(max_processes))
    process_slot = {}
    processes = set()
    process_logs = {}
    tmp_files = set()
    remove_jobs = set()
    log_dir = os.path.join(docker_home, "simulations", experiment_name, "logs")
    log_files = set()
    log_index = 0

    dont_collect = True

    def run_single_workload(suite, subsuite, workload, exp_cluster_id, sim_mode, warmup):
        nonlocal dont_collect
        nonlocal log_index
        try:
            docker_prefix = get_docker_prefix(sim_mode, workloads_data[suite][subsuite][workload]["simulation"])
            info(f"Using docker image with name {image_tag_for(docker_prefix, infra_dir)}", dbg_lvl)
            trace_warmup = None
            trace_type = ""
            trace_file = None
            env_vars = ""
            bincmd = ""
            client_bincmd = ""
            seg_size = None
            simulation_data = workloads_data[suite][subsuite][workload]["simulation"][sim_mode]
            if sim_mode == "memtrace":
                trace_warmup = simulation_data["warmup"]
                trace_type = simulation_data["trace_type"]
                trace_file = simulation_data["whole_trace_file"]
                seg_size = simulation_data["segment_size"]
            if sim_mode == "pt":
                trace_warmup = simulation_data["warmup"]
            if sim_mode == "exec":
                require_aslr_disabled(dbg_lvl)
                env_vars = simulation_data["env_vars"]
                bincmd = simulation_data["binary_cmd"]
                client_bincmd = simulation_data["client_bincmd"]
                seg_size = simulation_data["segment_size"]

            if "simpoints" not in workloads_data[suite][subsuite][workload].keys():
                weight = 1
                simpoints = {}
                simpoints["0"] = weight
            elif exp_cluster_id == None:
                simpoints = get_simpoints(workloads_data[suite][subsuite][workload], sim_mode, dbg_lvl)
            elif exp_cluster_id >= 0:
                weight = get_weight_by_cluster_id(exp_cluster_id, workloads_data[suite][subsuite][workload]["simpoints"])
                simpoints = {}
                simpoints[f"{exp_cluster_id}"] = weight

            for config_key in configs:
                config = configs[config_key]["params"]
                scarab_binary = configs[config_key]["binary"]
                if config == "":
                    config = None

                for cluster_id, weight in simpoints.items():
                    info(f"cluster_id: {cluster_id}, weight: {weight}", dbg_lvl)
                    
                    dont_collect = False

                    docker_container_name = f"{docker_prefix}_{suite}_{subsuite}_{workload}_{experiment_name}_{config_key.replace("/", "-")}_{cluster_id}_{sim_mode}_{user}"
                    # Create temp file with run command and run it
                    filename = f"{docker_container_name}_tmp_run.sh"

                    if check_can_skip(descriptor_data, config_key, suite, subsuite, workload, cluster_id, filename, dbg_lvl=dbg_lvl):
                        info(f"Skipping {workload} with config {config_key} and cluster id {cluster_id}", dbg_lvl)
                        continue

                    workload_home = f"{suite}/{subsuite}/{workload}"
                    slot = heapq.heappop(free_slots)
                    pool_container = None
                    if container_reuse and pool_eligible(sim_mode, docker_prefix, infra_dir):
                        pool_container = pool_container_name(docker_prefix, slot, experiment_name, user)
                    write_docker_command_to_file(user, local_uid, local_gid, workload, workload_home, experiment_name,
                                                 docker_prefix, docker_container_name, traces_dir,
                                                 docker_home, config_key, config, sim_mode, scarab_binary,
                                                 seg_size, architecture, cluster_id, warmup, trace_warmup, trace_type,
                                                 trace_file, env_vars, bincmd, client_bincmd, filename, infra_dir, application_dir, perf_stat=bool(descriptor_data.get("perf_stat", False)),
                                                 pool_container=pool_container)
                    tmp_files.add(filename)
                    command = '/bin/bash ' + filename
                    remove_jobs.add((config_key, suite, subsuite, workload, cluster_id))
                    log_path = os.path.join(
                        log_dir,
                        f"job_{log_index}.out",
                    )
                    log_index += 1
                    log_handle = open(log_path, "w")
                    log_files.add(log_handle)
                    process = subprocess.Popen(
                        "exec " + command,
                        stdout=log_handle,
                        stderr=log_handle,
                        shell=True,
                    )
                    processes.add(process)
                    process_logs[process] = log_handle
                    process_slot[process] = slot
                    info(f"Running command '{command}'", dbg_lvl)
                    while len(processes) >= max_processes:
                        # Loop through the processes and wait for one to finish
                        for p in processes.copy():
                            if p.poll() is not None: # This process has finished
                                p.wait() # Make sure it's really finished
                                processes.remove(p) # Remove from set of active processes
                                heapq.heappush(free_slots, process_slot.pop(p))
                                handle = process_logs.pop(p, None)
                                if handle:
                                    handle.close()
                                break # Exit the loop after removing one process
                        else:
                            # Nothing has finished yet. Simulations run for minutes to hours,
                            # so poll once a second instead of spinning a core at 100%.
                            time.sleep(1)
        except Exception as e:
            raise e

    try:
        # Get a local user/group ids
        local_uid = os.getuid()
        local_gid = os.getgid()

        scarab_binaries = []
        for sim in simulations:
            for config_key in configs:
                scarab_binary = configs[config_key]["binary"]
                if scarab_binary not in scarab_binaries:
                    scarab_binaries.append(scarab_binary)

        try:
            scarab_githash, image_tag_list = prepare_simulation(user, scarab_path, scarab_build, descriptor_data['root_dir'], experiment_name, architecture, docker_prefix_list, infra_dir, scarab_binaries, interactive_shell=False, dbg_lvl=dbg_lvl)
        except RuntimeError as e:
            # This error prints a message. Now stop execution
            return
        os.makedirs(os.path.join(docker_home, "simulations", experiment_name, "logs"), exist_ok=True)

        # Reused containers left by an earlier run of this experiment whose runner was killed,
        # except those a surviving job script is still running its sim in.
        remove_pool_containers(docker_prefix_list, experiment_name, user, dbg_lvl, stale=True)

        # Collect old job logs before submitting new jobs
        experiment_dir = f"{descriptor_data['root_dir']}/simulations/{experiment_name}"
        old_job_logs = get_old_job_logs(f"{experiment_dir}/logs")

        print("Submitting jobs...")
        # Iterate over each workload and config combo
        simulations = normalize_simulations(simulations)
        for simulation in simulations:
            suite = simulation["suite"]
            subsuite = simulation["subsuite"]
            workload = simulation["workload"]
            exp_cluster_id = simulation["cluster_id"]
            sim_mode = simulation["simulation_type"]
            sim_warmup = simulation["warmup"]

            # Run all the workloads within suite
            if workload == None and subsuite == None:
                for subsuite_ in workloads_data[suite].keys():
                    for workload_ in workloads_data[suite][subsuite_].keys():
                        if not isinstance(workloads_data[suite][subsuite_][workload_], dict):
                            continue
                        sim_mode_ = sim_mode
                        if sim_mode_ == None:
                            sim_mode_ = workloads_data[suite][subsuite_][workload_]["simulation"]["prioritized_mode"]
                        if sim_warmup == None:  # Use the whole warmup available in the trace if not specified
                            sim_warmup = workloads_data[suite][subsuite_][workload_]["simulation"][sim_mode_]["warmup"]
                        run_single_workload(suite, subsuite_, workload_, exp_cluster_id, sim_mode_, sim_warmup)
            elif workload == None and subsuite != None:
                for workload_ in workloads_data[suite][subsuite].keys():
                    if not isinstance(workloads_data[suite][subsuite][workload_], dict):
                        continue
                    sim_mode_ = sim_mode
                    if sim_mode_ == None:
                        sim_mode_ = workloads_data[suite][subsuite][workload_]["simulation"]["prioritized_mode"]
                    if sim_warmup == None:  # Use the whole warmup available in the trace if not specified
                        sim_warmup = workloads_data[suite][subsuite][workload_]["simulation"][sim_mode_]["warmup"]
                    run_single_workload(suite, subsuite, workload_, exp_cluster_id, sim_mode_, sim_warmup)
            elif workload != None and subsuite == None:
                found = False
                for subsuite_ in workloads_data[suite].keys():
                    if not workload in workloads_data[suite][subsuite_].keys():
                        continue
                    found = True
                    _subsuite = subsuite_
                assert found, f"Workload {workload} could not be found for any subsuite of {suite}. Check descriptor validation code"
                sim_mode_ = sim_mode
                if sim_mode_ == None:
                    sim_mode_ = workloads_data[suite][_subsuite][workload]["simulation"]["prioritized_mode"]
                if sim_warmup == None:  # Use the whole warmup available in the trace if not specified
                    sim_warmup = workloads_data[suite][_subsuite][workload]["simulation"][sim_mode_]["warmup"]
                run_single_workload(suite, _subsuite, workload, exp_cluster_id, sim_mode_, sim_warmup)
            else:
                sim_mode_ = sim_mode
                if sim_mode_ == None:
                    sim_mode_ = workloads_data[suite][subsuite][workload]["simulation"]["prioritized_mode"]
                if sim_warmup == None:  # Use the whole warmup available in the trace if not specified
                    sim_warmup = workloads_data[suite][subsuite][workload]["simulation"][sim_mode_]["warmup"]
                run_single_workload(suite, subsuite, workload, exp_cluster_id, sim_mode_, sim_warmup)

        print("Wait processes...")
        for p in processes:
            p.wait()

        if container_reuse:
            remove_pool_containers(docker_prefix_list, experiment_name, user, dbg_lvl, runner_pid=os.getpid())

        for p in processes:
            log_handle = process_logs.get(p)
            if log_handle:
                log_handle.close()

        # Clean up temp files
        for tmp in tmp_files:
            info(f"Removing temporary run script {tmp}", dbg_lvl)
            os.remove(tmp)

        remove_old_job_logs(old_job_logs, remove_jobs, dbg_lvl)

        finish_simulation(user, docker_home, descriptor_path, descriptor_data['root_dir'], experiment_name, image_tag_list, [], dont_collect=dont_collect)

    except Exception as e:
        print("An exception occurred:", e)
        traceback.print_exc()  # Print the full stack trace
        for p in processes:
            p.kill()

        for handle in log_files:
            handle.close()

        # Clean up temp files
        for tmp in tmp_files:
            info(f"Removing temporary run script {tmp}", dbg_lvl)
            os.remove(tmp)

        remove_old_job_logs(old_job_logs, remove_jobs, dbg_lvl)

        infra_dir = subprocess.check_output(["pwd"]).decode("utf-8").split("\n")[0]
        print(infra_dir)
        kill_jobs(user, "simulation", experiment_name, docker_prefix_list, infra_dir, dbg_lvl,
                  list(configs))
    finally:
        # Ctrl-C reaches the job scripts too, and each takes its slot's container down, but idle
        # slots' containers would stay. Normal exits removed them above; this finds none then.
        if container_reuse:
            remove_pool_containers(docker_prefix_list, experiment_name, user, dbg_lvl, runner_pid=os.getpid())

def run_tracing(user, descriptor_data, workload_db_path, infra_dir, dbg_lvl = 2):
    trace_name = descriptor_data["trace_name"]
    docker_home = descriptor_data["root_dir"]
    scarab_path = descriptor_data["scarab_path"]
    scarab_build = descriptor_data["scarab_build"]
    traces_dir = descriptor_data["traces_dir"]
    trace_configs = descriptor_data["trace_configurations"]
    application_dir = descriptor_data["application_dir"]

    docker_prefix_list = []
    for config in trace_configs:
        image_name = config["image_name"]
        if image_name not in docker_prefix_list:
            docker_prefix_list.append(image_name)

    available_cores = os.cpu_count()
    max_processes = int(available_cores * 0.9)
    processes = set()
    tmp_files = set()
    log_files = set()

    def run_single_trace(workload, image_name, trace_name, env_vars, binary_cmd, client_bincmd, trace_type, drio_args, clustering_k, infra_dir, application_dir):
        try:
            if trace_type == "cluster_then_trace":
                simpoint_mode = "cluster_then_trace"
            elif trace_type == "trace_then_cluster":
                simpoint_mode = "trace_then_post_process"
            elif trace_type == "iterative_trace":
                simpoint_mode = "iterative_trace"
            elif trace_type == "cluster_only":
                simpoint_mode = "cluster_only"
            elif trace_type == "trace_single_segment":
                simpoint_mode = "trace_single_segment"
            elif trace_type == "pinball_extraction":
                simpoint_mode = "pinball_extraction"
            else:
                raise Exception(f"Invalid trace type: {trace_type}")
            info(f"Using docker image with name {image_tag_for(image_name, infra_dir)}", dbg_lvl)
            docker_container_name = f"{image_name}_{workload}_{trace_name}_{simpoint_mode}_{user}"
            filename = f"{docker_container_name}_tmp_run.sh"
            write_trace_docker_command_to_file(user, local_uid, local_gid, docker_container_name,
                                               workload, image_name, trace_name, traces_dir, docker_home,
                                               env_vars, binary_cmd, client_bincmd, simpoint_mode, drio_args,
                                               clustering_k, filename, infra_dir, application_dir)
            tmp_files.add(filename)
            command = '/bin/bash ' + filename
            subprocess.run(["mkdir", "-p", f"{docker_home}/simpoint_flow/{trace_name}/{workload}"], check=True, capture_output=True, text=True)
            log_out = f"{docker_home}/simpoint_flow/{trace_name}/{workload}/log.out"
            log_err = f"{docker_home}/simpoint_flow/{trace_name}/{workload}/log.err"
            out = open(log_out, "w")
            err = open(log_err, "w")
            log_files.add((out, err))
            process = subprocess.Popen(command, stdout=out, stderr=err, shell=True, preexec_fn=os.setsid)
            processes.add(process)
            info(f"Running command '{command}'", dbg_lvl)
        except Exception as e:
            raise e

    try:
        # Get a local user/group ids
        local_uid = os.getuid()
        local_gid = os.getgid()


        prepare_trace(user, scarab_path, scarab_build, docker_home, trace_name, infra_dir, docker_prefix_list, False, [], dbg_lvl=dbg_lvl)

        # Iterate over each trace configuration
        for config in trace_configs:
            workload = config["workload"]
            image_name = config["image_name"]
            if config["env_vars"] != None:
                env_vars = config["env_vars"].split()
            else:
                env_vars = config["env_vars"]
            binary_cmd = config["binary_cmd"]
            client_bincmd = config["client_bincmd"]
            trace_type = config["trace_type"]
            drio_args = config["dynamorio_args"]
            clustering_k = config["clustering_k"]

            run_single_trace(workload, image_name, trace_name, env_vars, binary_cmd, client_bincmd,
                             trace_type, drio_args, clustering_k, infra_dir, application_dir)

        print("Wait processes...")
        for p in processes:
            p.wait()

        for out, err in log_files:
            out.close()
            err.close()

        # Clean up temp files
        for tmp in tmp_files:
            info(f"Removing temporary run script {tmp}", dbg_lvl)
            os.remove(tmp)

        finish_trace(user, descriptor_data, workload_db_path, infra_dir, dbg_lvl)
    except Exception as e:
        print("An exception occurred:", e)
        traceback.print_exc()  # Print the full stack trace
        for p in processes:
            p.kill()

        for out, err in log_files:
            out.close()
            err.close()

        # Clean up temp files
        for tmp in tmp_files:
            info(f"Removing temporary run script {tmp}", dbg_lvl)
            os.remove(tmp)

        kill_jobs(user, "trace", trace_name, docker_prefix_list, infra_dir, dbg_lvl)

