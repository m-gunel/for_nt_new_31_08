    bash_check()
    {
      echo Проверка при помощи bash
      while ! timeout 1 bash -c "</dev/tcp/localhost/15021" >/dev/null 2>&1 ; do echo -n . ; sleep 1 ; done
      echo Успешно
    }

    nc_check()
    {
      echo Проверка при помощи nc
      while ! timeout 1  nc -zv 127.0.0.1:15021 ; do echo -n . ; sleep 1 ; done
      echo Успешно
    }

    waiting_istio()
    {
      echo -n Waiting istio
      if [ `command -v bash` ]; then
        bash_check
      else
        if [ `command -v nc` ]; then
          nc_check
        else
          echo Не найдено ни одного инструмента проверки доступности истио!
          exit 1
        fi
      fi
    }

    waiting_secman(){
      config_file=/opt/secman-wait-config/secman.txt
      checked_files=`cat $config_file`
      files_count=0
      for file in $checked_files ; do
      files_count=$(( files_count + 1 ))
      done
      exists_files_count=0
      time_counter=0
      while [ $exists_files_count != $files_count ]; do
      exists_files_count=0
      for file in $checked_files ; do
      if [ -f $file ]; then
      exists_files_count=$(( exists_files_count + 1 ))
      chmod 0600 $file
      fi
      done
      if [ "$time_counter" -gt 60 ]; then
      echo "Время ожидание истекло."
      exit 1
      fi
      sleep 1
      time_counter=$(( time_counter + 1 ))
      echo "Ожидание SecMan $time_counter с."
      done
      echo "Ожидание закончилось."
    }

    if [[ "$ISTIO" -eq "TRUE"  &&  "$WAIT_ENVOY" -eq "TRUE" ]]
    then
      waiting_istio
    fi
    if [[ "$SECMAN" -eq "TRUE" ]]; then
      waiting_secman
    fi
    $COMMAND